"""Minimal HTTP service for a transfer-only ByteSqueeze worker.

The controller owns the media library. A worker receives short-lived download
grants, stages sources under /work, runs HandBrake locally, and uploads results
to the controller. No media-library mount or browser UI is exposed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, request

from webui.app.events import log_event
from webui.app.jobs import (
    cancel_job,
    clear_finished_jobs,
    clear_queued_jobs,
    create_remote_transfer_job,
    get_job,
    get_job_summary,
    get_queue_state,
    initialize_jobs_system,
    list_jobs_for_api,
    move_queued_job,
    move_queued_job_to_position,
    read_job_log,
    remove_queued_job,
    replace_queued_job_preset,
    set_queue_paused,
)
from webui.app.node_linking import (
    NODE_PROTOCOL_VERSION,
    accept_pairing,
    create_pairing_code,
    delete_trusted_controller,
    enable_pair_recovery,
    list_trusted_controllers_public,
    local_node_overview,
    node_discovery,
    recover_pairing,
    set_local_node_name,
    trusted_controller,
    update_trusted_controller,
    verify_hmac,
)
from webui.app.presets import guess_preset_from_filename, load_preset_config
from webui.app.settings import load_settings, normalize_output_container, save_settings


WORKER_RELEASE = "2.11.1"


def _public_encoding_policy() -> dict:
    settings = load_settings()
    output_container = normalize_output_container(settings.get("output_container"))
    return {
        "hb_threads": int(settings.get("hb_threads") or 0),
        "hardware_transcode_concurrency": int(
            settings.get("hardware_transcode_concurrency") or 1
        ),
        "software_transcode_concurrency": 1,
        "software_jobs_are_exclusive": True,
        "controller_managed": bool(
            settings.get("worker_controller_managed_capacity", False)
        ),
        "auto_stop_large_output_enabled": bool(
            settings.get("auto_stop_large_output_enabled", False)
        ),
        "auto_stop_large_output_percent": settings.get(
            "auto_stop_large_output_percent",
            90,
        ),
        "output_container": output_container,
        "web_optimized": bool(
            output_container == "mp4" and settings.get("web_optimized", False)
        ),
        "audio_policy_default": str(settings.get("audio_policy_default") or "preserve"),
        "audio_optimize_codec": str(settings.get("audio_optimize_codec") or "aac"),
        "audio_optimize_bitrate_kbps": int(settings.get("audio_optimize_bitrate_kbps") or 1024),
        "audio_deduplicate_languages": bool(settings.get("audio_deduplicate_languages", True)),
        "audio_preferred_languages": list(settings.get("audio_preferred_languages") or ["eng", "spa"]),
        "audio_allow_downmix": bool(settings.get("audio_allow_downmix", False)),
        "audio_allow_object_metadata_loss": bool(settings.get("audio_allow_object_metadata_loss", False)),
    }


def _apply_controller_encoding_policy(policy: dict | None) -> dict:
    policy = policy if isinstance(policy, dict) else {}
    allowed = {
        "hb_threads",
        "hardware_transcode_concurrency",
        "auto_stop_large_output_enabled",
        "auto_stop_large_output_percent",
        "output_container",
        "web_optimized",
        "audio_policy_default",
        "audio_optimize_codec",
        "audio_optimize_bitrate_kbps",
        "audio_deduplicate_languages",
        "audio_preferred_languages",
        "audio_allow_downmix",
        "audio_allow_object_metadata_loss",
    }
    updates = {key: policy[key] for key in allowed if key in policy}
    if "hardware_transcode_concurrency" in updates:
        updates["worker_controller_managed_capacity"] = True
    if updates:
        save_settings(updates)
    return _public_encoding_policy()


def _work_dir() -> str:
    return os.path.abspath(os.environ.get("TSD_WORKER_TEMP_DIR") or "/work/jobs")


def _pairing_ttl() -> int:
    try:
        value = int(os.environ.get("TSD_WORKER_PAIRING_TTL_SECONDS") or 3600)
    except (TypeError, ValueError):
        value = 3600
    return max(300, min(3600, value))


def _print_pairing_banner(pairing: dict) -> None:
    code = str(pairing.get("code") or "")
    expires_at = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(float(pairing.get("expires_at") or 0)))
    print("", flush=True)
    print("=" * 68, flush=True)
    print(" ByteSqueeze headless worker is ready", flush=True)
    print(f" Pairing code:  {code}", flush=True)
    print(f" Code expires:  {expires_at}", flush=True)
    print(" On the main server open Settings > Linked Workers,", flush=True)
    print(" enter this worker's URL and the code above, then click Pair.", flush=True)
    print(" No media drives are required. Temporary jobs use /work.", flush=True)
    print("=" * 68, flush=True)
    print("", flush=True)


def _print_startup_diagnostics(work_dir: str) -> None:
    usage = shutil.disk_usage(work_dir)
    print(
        f"[WORKER] release={WORKER_RELEASE} name={os.environ.get('TSD_WORKER_NAME') or 'ByteSqueeze Worker'}",
        flush=True,
    )
    print(
        f"[WORKER] work_dir={work_dir} free_bytes={int(usage.free)} total_bytes={int(usage.total)}",
        flush=True,
    )
    print(
        f"[WORKER] HandBrakeCLI={shutil.which('HandBrakeCLI') or 'NOT FOUND'} "
        f"ffprobe={shutil.which('ffprobe') or 'NOT FOUND'}",
        flush=True,
    )
    print(
        f"[WORKER] qsv_render_device="
        f"{os.environ.get('TSD_QSV_RENDER_DEVICE') or '/dev/dri/renderD128'} "
        f"va_driver={os.environ.get('LIBVA_DRIVER_NAME') or 'iHD'} "
        f"qsv_adapter={os.environ.get('TSD_QSV_ADAPTER') or '0'}",
        flush=True,
    )
    print(
        "[WORKER] Create a fresh pairing code at any time with: "
        "python -m worker.app pairing-code",
        flush=True,
    )


def _request_scheme() -> str:
    value = str(request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",")[0].strip().lower()
    return value if value in {"http", "https"} else "http"


def _remote_request_host() -> str:
    host = str(request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    return host.strip("[]")


def _infer_controller_url(advertised_url: str = "") -> str:
    host = _remote_request_host()
    if not host:
        return ""
    advertised = urlparse("")
    try:
        advertised = urlparse(str(advertised_url or "").strip())
        scheme = advertised.scheme if advertised.scheme in {"http", "https"} else _request_scheme()
        port = advertised.port
    except Exception:
        scheme = _request_scheme()
        port = None
    if host in {"127.0.0.1", "::1", "localhost", "0.0.0.0"}:
        return ""
    if (
        scheme == "https"
        and advertised.hostname
        and advertised.hostname.strip("[]").lower() != host.lower()
    ):
        # Replacing a TLS hostname with an observed IP would normally fail
        # certificate validation. Keep the advertised HTTPS endpoint instead.
        return ""
    host_text = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    port_text = f":{port}" if port and port != default_port else ""
    return f"{scheme}://{host_text}{port_text}"


def _authenticated_controller():
    node_id = request.headers.get("X-Node-Id") or ""
    timestamp = request.headers.get("X-Node-Timestamp") or ""
    signature = request.headers.get("X-Node-Signature") or ""
    controller = trusted_controller(node_id)
    if not controller:
        return None
    body_bytes = request.get_data(cache=True) or b""
    if not verify_hmac(
        request.method,
        request.path,
        body_bytes,
        node_id=node_id,
        token=str(controller.get("token") or ""),
        timestamp=timestamp,
        signature=signature,
    ):
        return None
    updates = {"last_seen": time.time()}
    observed_url = _infer_controller_url(controller.get("url") or "")
    if observed_url:
        updates["observed_url"] = observed_url
    update_trusted_controller(node_id, updates)
    return {**controller, **updates}


def create_worker_app(*, announce_pairing: bool = True) -> Flask:
    """Create the worker-only Flask application."""
    app = Flask(__name__)
    work_dir = _work_dir()
    os.makedirs(work_dir, exist_ok=True)

    worker_name = str(os.environ.get("TSD_WORKER_NAME") or "ByteSqueeze Worker").strip()[:80]
    set_local_node_name(worker_name)
    save_settings({"remote_transfer_temp_dir": work_dir})
    load_preset_config()
    initialize_jobs_system()

    if announce_pairing:
        _print_startup_diagnostics(work_dir)
        pairing = create_pairing_code(ttl_seconds=_pairing_ttl())
        _print_pairing_banner(pairing)

    @app.get("/")
    def worker_root():
        local = local_node_overview()
        return jsonify(
            ok=True,
            service="bytesqueeze-headless-worker",
            release=WORKER_RELEASE,
            node_id=local.get("id"),
            node_name=local.get("name"),
            paired=bool(local.get("paired_controller_count")),
            encoding_policy=_public_encoding_policy(),
            message=(
                "Headless worker only. Pairing codes are printed in docker logs; "
                "run 'python -m worker.app pairing-code' in the container for a fresh code."
            ),
        )

    @app.get("/api/health")
    def worker_health():
        usage = shutil.disk_usage(work_dir)
        summary = get_job_summary()
        return jsonify(
            ok=True,
            status="healthy",
            release=WORKER_RELEASE,
            service="bytesqueeze-headless-worker",
            work={"path": work_dir, "free_bytes": int(usage.free), "total_bytes": int(usage.total)},
            queue=summary,
            encoding_policy=_public_encoding_policy(),
        )

    @app.get("/api/node/discovery")
    def worker_discovery():
        return jsonify(ok=True, **node_discovery())

    @app.post("/api/node/pair/accept")
    def worker_pair_accept():
        data = request.get_json(silent=True) or {}
        advertised_url = str(data.get("controller_url") or "").strip().rstrip("/")
        observed_url = _infer_controller_url(advertised_url)
        if advertised_url:
            data["advertised_controller_url"] = advertised_url
        if observed_url:
            data["observed_controller_url"] = observed_url
            data["controller_url"] = observed_url
        elif not advertised_url:
            data["controller_url"] = _infer_controller_url()
        try:
            accepted = accept_pairing(data.get("code") or "", data)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        accepted["capabilities"] = node_discovery().get("capabilities") or []
        accepted["hardware"] = node_discovery().get("hardware") or {}
        print(
            f"[WORKER] Paired with controller {data.get('controller_name') or data.get('controller_id')} "
            f"at {accepted.get('controller_url') or data.get('controller_url')}",
            flush=True,
        )
        log_event("node_paired", "Controller paired with this headless worker.", level="info")
        return jsonify(ok=True, worker_mode="headless", requires_remote_transfer=True, **accepted)

    @app.post("/api/node/pair/recover")
    def worker_pair_recover():
        data = request.get_json(silent=True) or {}
        advertised_url = str(data.get("controller_url") or "").strip().rstrip("/")
        observed_url = _infer_controller_url(advertised_url)
        if advertised_url:
            data["advertised_controller_url"] = advertised_url
        if observed_url:
            data["observed_controller_url"] = observed_url
            data["controller_url"] = observed_url
        elif not advertised_url:
            data["controller_url"] = _infer_controller_url()
        try:
            recovered = recover_pairing(data)
        except ValueError as exc:
            return jsonify(error=str(exc)), 401
        recovered["capabilities"] = node_discovery().get("capabilities") or []
        recovered["hardware"] = node_discovery().get("hardware") or {}
        return jsonify(ok=True, worker_mode="headless", requires_remote_transfer=True, **recovered)

    @app.post("/api/node/pair/enable-recovery")
    def worker_enable_recovery():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        try:
            token = enable_pair_recovery(str(controller.get("id") or ""))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, recovery_token=token)

    @app.get("/api/node/status")
    def worker_status():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        local = local_node_overview()
        usage = shutil.disk_usage(work_dir)
        discovery = node_discovery()
        return jsonify(
            ok=True,
            release=WORKER_RELEASE,
            id=local.get("id"),
            name=local.get("name"),
            role="worker",
            role_label="Headless transfer worker",
            worker_mode="headless",
            requires_remote_transfer=True,
            protocol_version=NODE_PROTOCOL_VERSION,
            capabilities=discovery.get("capabilities") or [],
            hardware=discovery.get("hardware") or {},
            paired_controllers=list_trusted_controllers_public(),
            remote_transfer_temp_dir=work_dir,
            work_free_bytes=int(usage.free),
            summary=get_job_summary(),
            jobs=list_jobs_for_api(include_log_tail=True),
            encoding_policy=_public_encoding_policy(),
            prediction_profile={},
        )

    @app.post("/api/node/config")
    def worker_controller_config():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        policy = request.get_json(silent=True) or {}
        if not any(
            key in policy
            for key in (
                "hb_threads",
                "hardware_transcode_concurrency",
                "auto_stop_large_output_enabled",
                "auto_stop_large_output_percent",
                "audio_policy_default",
                "audio_optimize_codec",
                "audio_optimize_bitrate_kbps",
                "audio_deduplicate_languages",
                "audio_preferred_languages",
                "audio_allow_downmix",
                "audio_allow_object_metadata_loss",
            )
        ):
            return jsonify(error="no supported worker settings supplied"), 400
        applied = _apply_controller_encoding_policy(policy)
        log_event(
            "worker_capacity_updated",
            f"{controller.get('name') or 'Controller'} set this worker to "
            f"{applied['hardware_transcode_concurrency']} GPU slot(s).",
            level="info",
        )
        return jsonify(ok=True, encoding_policy=applied)

    @app.post("/api/node/jobs")
    def worker_receive_jobs():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        payload = request.get_json(silent=True) or {}
        jobs_payload = payload.get("jobs")
        if not isinstance(jobs_payload, list):
            return jsonify(error="missing jobs"), 400
        applied_policy = _apply_controller_encoding_policy(
            payload.get("encoding_policy")
        )

        count = 0
        skipped = []
        for job in jobs_payload:
            if not isinstance(job, dict):
                continue
            transfer = job.get("transfer") if isinstance(job.get("transfer"), dict) else {}
            src = str(job.get("src") or transfer.get("original_path") or transfer.get("source_basename") or "").strip()
            if not transfer:
                skipped.append({"path": src, "reason": "headless worker requires remote transfer"})
                continue
            missing = [
                key
                for key in ("source_url", "upload_url", "download_token", "upload_token", "worker_node_id")
                if not str(transfer.get(key) or "").strip()
            ]
            if missing:
                skipped.append({"path": src, "reason": f"missing transfer data: {', '.join(missing)}"})
                continue
            transfer["remote_temp_dir"] = work_dir
            preset = str(job.get("preset") or "auto").strip().lower()
            if preset not in {"auto", "1080", "4k"}:
                preset = "auto"
            effective = guess_preset_from_filename(os.path.basename(src)) if preset == "auto" else preset
            _job_id, created = create_remote_transfer_job(
                src,
                effective,
                transfer,
                extra_args=str(job.get("extra_args") or ""),
                preset_bundle=job.get("preset_bundle"),
                encode_metadata=job.get("encode_metadata") if isinstance(job.get("encode_metadata"), dict) else None,
                encoding_policy=(
                    job.get("encoding_policy")
                    if isinstance(job.get("encoding_policy"), dict)
                    else applied_policy
                ),
            )
            count += 1 if created else 0

        print(f"[WORKER] Accepted {count} remote job(s); skipped {len(skipped)}.", flush=True)
        log_event("node_jobs_received", f"Received {count} headless worker job(s).", level="info")
        return jsonify(
            ok=True,
            count=count,
            skipped=skipped,
            summary=get_job_summary(),
            encoding_policy=applied_policy,
        )

    @app.get("/api/node/jobs/<job_id>/log")
    def worker_job_log(job_id):
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        job = get_job(job_id)
        if not job:
            return jsonify(error="job not found"), 404
        try:
            contents, truncated = read_job_log(job_id)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(
            ok=True,
            job_id=job_id,
            status=job.get("status"),
            error_message=job.get("error_message") or "",
            log=contents,
            truncated=truncated,
        )

    @app.post("/api/node/jobs/<job_id>/action")
    def worker_job_action(job_id):
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or "").strip().lower()
        if action == "cancel":
            ok, error = cancel_job(job_id)
        elif action == "remove":
            ok, error = remove_queued_job(job_id)
        elif action in {"up", "down", "top", "bottom"}:
            ok, error = move_queued_job(job_id, action)
        elif action == "position":
            ok, error = move_queued_job_to_position(job_id, data.get("position"))
        else:
            return jsonify(error="invalid job action"), 400
        if not ok:
            return jsonify(error=error or "job action failed"), 400
        return jsonify(
            ok=True,
            job_id=job_id,
            action=action,
            jobs=list_jobs_for_api(include_log_tail=True),
            summary=get_job_summary(),
        )

    @app.post("/api/node/jobs/<job_id>/preset")
    def worker_job_preset(job_id):
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        data = request.get_json(silent=True) or {}
        plan = data.get("plan") if isinstance(data.get("plan"), dict) else None
        ok, error = replace_queued_job_preset(job_id, plan)
        if not ok:
            return jsonify(error=error or "preset edit failed"), 400
        return jsonify(
            ok=True,
            job=next(
                (row for row in list_jobs_for_api(include_log_tail=True) if row.get("id") == job_id),
                None,
            ),
            summary=get_job_summary(),
        )

    @app.post("/api/node/jobs/clear")
    def worker_jobs_clear():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        data = request.get_json(silent=True) or {}
        target = str(data.get("target") or "finished").strip().lower()
        if target == "finished":
            removed = clear_finished_jobs()
        elif target == "queued":
            removed = clear_queued_jobs()
        else:
            return jsonify(error="target must be finished or queued"), 400
        log_event(
            "worker_jobs_cleared",
            f"{controller.get('name') or 'Controller'} cleared {removed} {target} worker job(s).",
            level="info",
        )
        return jsonify(
            ok=True,
            removed=removed,
            jobs=list_jobs_for_api(include_log_tail=True),
            summary=get_job_summary(),
        )

    @app.post("/api/node/queue")
    def worker_queue_control():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        data = request.get_json(silent=True) or {}
        if not isinstance(data.get("paused"), bool):
            return jsonify(error="paused must be true or false"), 400
        paused = set_queue_paused(data["paused"])
        return jsonify(
            ok=True,
            paused=paused,
            queue_paused=get_queue_state(),
            summary=get_job_summary(),
        )

    @app.post("/api/node/rotate_secret")
    def worker_rotate_secret():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        data = request.get_json(silent=True) or {}
        new_token = str(data.get("token") or "").strip()
        if len(new_token) < 24:
            return jsonify(error="invalid token"), 400
        update_trusted_controller(controller["id"], {"token": new_token})
        return jsonify(ok=True)

    @app.post("/api/node/unlink")
    def worker_unlink():
        controller = _authenticated_controller()
        if not controller:
            return jsonify(error="unauthorized"), 401
        delete_trusted_controller(controller["id"])
        pairing = create_pairing_code(ttl_seconds=_pairing_ttl())
        _print_pairing_banner(pairing)
        return jsonify(ok=True)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ByteSqueeze headless worker tools")
    subcommands = parser.add_subparsers(dest="command", required=True)
    pairing_parser = subcommands.add_parser(
        "pairing-code",
        help="generate and print a new one-time controller pairing code",
    )
    pairing_parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        help="code lifetime (300-3600 seconds; defaults to worker configuration)",
    )
    args = parser.parse_args(argv)
    if args.command == "pairing-code":
        ttl = _pairing_ttl() if args.ttl_seconds is None else max(300, min(3600, args.ttl_seconds))
        pairing = create_pairing_code(ttl_seconds=ttl)
        _print_pairing_banner(pairing)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
