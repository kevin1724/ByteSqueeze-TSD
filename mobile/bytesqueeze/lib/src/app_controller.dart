import 'dart:async';

import 'package:flutter/foundation.dart';

import 'api_client.dart';
import 'demo_data.dart';
import 'quick_pairing.dart';
import 'session_store.dart';

class AppController extends ChangeNotifier {
  AppController() : store = SessionStore() {
    api = ByteSqueezeApi(store);
  }

  final SessionStore store;
  late ByteSqueezeApi api;

  bool booting = true;
  bool busy = false;
  bool demoMode = false;
  String? error;
  bool serverSupportsOperationsSettings = true;
  int selectedTab = 0;
  ServerSession? session;
  String interfaceVersion = 'v3';
  String interfaceDensity = 'comfortable';
  bool showSecondaryUi = false;
  bool statsForNerds = false;
  String quickPairServer = '';
  String quickPairCode = '';
  QuickPairingRequest? _pendingQuickPairing;

  Map<String, dynamic> dashboard = {};
  Map<String, dynamic> jobs = {};
  Map<String, dynamic> library = {};
  Map<String, dynamic> calendar = {};
  Map<String, dynamic> automation = {};
  Map<String, dynamic> nodes = {};
  Map<String, dynamic> storage = {};
  Map<String, dynamic> events = {};
  Map<String, dynamic> smartPresets = {};
  Map<String, dynamic> autopilotReview = {};
  Map<String, dynamic> operations = {};

  bool get connected => demoMode || session != null;
  bool get canControl => demoMode || session?.canControl == true;
  bool get useV3 => interfaceVersion == 'v3';
  bool get compactInterface => interfaceDensity == 'compact';
  String get serverLabel => demoMode
      ? 'Demo server'
      : (api.activeBaseUrl.isEmpty ? 'Not connected' : api.activeBaseUrl);

  Future<void> bootstrap() async {
    booting = true;
    notifyListeners();
    interfaceVersion = await store.loadInterfaceVersion();
    interfaceDensity = await store.loadInterfaceDensity();
    showSecondaryUi = await store.loadShowSecondaryUi();
    statsForNerds = await store.loadStatsForNerds();
    session = await store.load();
    api.session = session;
    if (session != null) {
      try {
        await refreshAll(notifyBusy: false);
      } catch (_) {
        // Keep the saved session. The shell can show offline data and retry.
      }
    }
    booting = false;
    notifyListeners();
    final pending = _pendingQuickPairing;
    _pendingQuickPairing = null;
    if (pending != null) {
      unawaited(_connectQuickPairing(pending));
    }
  }

  Future<void> handleQuickPairingLink(String value) async {
    final request = QuickPairingRequest.tryParse(value);
    if (request == null) {
      error = 'This ByteSqueeze quick-pair link is invalid or incomplete.';
      notifyListeners();
      return;
    }
    quickPairServer = request.server;
    quickPairCode = request.code;
    error = null;
    notifyListeners();
    if (booting) {
      _pendingQuickPairing = request;
      return;
    }
    await _connectQuickPairing(request);
  }

  Future<void> _connectQuickPairing(QuickPairingRequest request) async {
    try {
      await pair(
        baseUrl: request.server,
        code: request.code,
        deviceName: session?.deviceName ?? 'ByteSqueeze Android',
      );
      quickPairCode = '';
    } catch (_) {
      // PairingScreen keeps the server/code filled in so the user can retry or
      // add an away/Tailscale address without generating another code.
    }
  }

  Future<void> pair(
      {required String baseUrl,
      String fallbackBaseUrl = '',
      required String code,
      required String deviceName}) async {
    busy = true;
    error = null;
    notifyListeners();
    try {
      session = await api.pair(
          baseUrl: baseUrl,
          fallbackBaseUrl: fallbackBaseUrl,
          code: code,
          deviceName: deviceName);
      demoMode = false;
      await refreshAll(notifyBusy: false);
    } catch (failure) {
      error = _message(failure);
      rethrow;
    } finally {
      busy = false;
      notifyListeners();
    }
  }

  void enterDemo() {
    booting = false;
    demoMode = true;
    session = null;
    api.session = null;
    dashboard = DemoData.dashboard;
    jobs = DemoData.jobs;
    library = DemoData.library;
    calendar = DemoData.calendar;
    automation = DemoData.automation;
    nodes = DemoData.nodes;
    storage = DemoData.storage;
    events = DemoData.events;
    smartPresets = DemoData.smart;
    autopilotReview = {};
    operations = DemoData.operations;
    serverSupportsOperationsSettings = true;
    error = null;
    notifyListeners();
  }

  Future<void> disconnect() async {
    await api.disconnect();
    session = null;
    demoMode = false;
    selectedTab = 0;
    dashboard = {};
    jobs = {};
    library = {};
    calendar = {};
    automation = {};
    nodes = {};
    storage = {};
    events = {};
    smartPresets = {};
    autopilotReview = {};
    operations = {};
    serverSupportsOperationsSettings = true;
    notifyListeners();
  }

  void selectTab(int value) {
    selectedTab = value.clamp(0, 4).toInt();
    notifyListeners();
  }

  Future<void> setInterfaceVersion(String value) async {
    interfaceVersion = value == 'v2' ? 'v2' : 'v3';
    await store.saveInterfacePreferences(
      version: interfaceVersion,
      density: interfaceDensity,
    );
    notifyListeners();
  }

  Future<void> setInterfaceDensity(String value) async {
    interfaceDensity = value == 'compact' ? 'compact' : 'comfortable';
    await store.saveInterfacePreferences(
      version: interfaceVersion,
      density: interfaceDensity,
    );
    notifyListeners();
  }

  Future<void> refreshAll({bool notifyBusy = true}) async {
    if (demoMode) {
      enterDemo();
      return;
    }
    if (notifyBusy) {
      busy = true;
      notifyListeners();
    }
    error = null;
    final failures = <String>[];
    await Future.wait([
      _load('/dashboard', (value) => dashboard = value, failures),
      _load('/jobs', (value) => jobs = value, failures),
      _load('/library', (value) => library = _map(value['library']), failures),
      _load('/calendar?days=180', (value) => calendar = _map(value['calendar']),
          failures),
      _load('/automation', (value) => automation = value, failures),
      _load('/nodes', (value) => nodes = value, failures),
      _load('/storage?limit=100', (value) => storage = value, failures),
      _load('/events?limit=100', (value) => events = value, failures),
      _load('/smart_presets', (value) => smartPresets = value, failures),
      _loadOperations(failures),
      _load('/autopilot/review',
          (value) => autopilotReview = _map(value['review']), failures),
    ]);
    if (!serverSupportsOperationsSettings) {
      final summary = _map(jobs['summary']);
      operations = {
        'hardware_transcode_concurrency':
            summary['hardware_transcode_concurrency'] ?? 1,
        'qsv_device_available': summary['qsv_device_available'] == true,
        'auto_stop_large_output_enabled': false,
        'auto_stop_large_output_percent': 90,
        'audio_policy_default': 'preserve',
        'audio_optimize_codec': 'aac',
        'audio_allow_downmix': false,
        'audio_allow_object_metadata_loss': false,
      };
    }
    if (failures.isNotEmpty) error = failures.first;
    busy = false;
    notifyListeners();
  }

  Future<void> _load(
    String path,
    void Function(Map<String, dynamic>) apply,
    List<String> failures,
  ) async {
    try {
      apply(await api.get(path));
    } catch (failure) {
      failures.add(_message(failure));
    }
  }

  Future<void> _loadOperations(List<String> failures) async {
    try {
      final value = await api.get('/operations');
      operations = _map(value['settings']);
      serverSupportsOperationsSettings = true;
    } on ApiFailure catch (failure) {
      if (_unsupportedOperationsEndpoint(failure)) {
        // V3 mobile can still control older TSD servers. Only the encoder
        // capacity editor needs the newer endpoint, so a missing route must
        // not make the entire connected app look offline.
        serverSupportsOperationsSettings = false;
        return;
      }
      failures.add(_message(failure));
    } catch (failure) {
      failures.add(_message(failure));
    }
  }

  Future<void> setQueuePaused(bool paused) async {
    _requireControl();
    if (demoMode) {
      jobs['paused'] = paused;
      _map(dashboard['queue'])['paused'] = paused;
      notifyListeners();
      return;
    }
    await api.post('/queue', {'paused': paused});
    await refreshJobsAndDashboard();
  }

  Future<void> jobAction(String jobId, String action, {int? position}) async {
    _requireControl();
    if (demoMode) {
      final rows = _list(jobs['jobs']);
      if (action == 'cancel' || action == 'remove') {
        rows.removeWhere((row) => _map(row)['id'] == jobId);
        jobs['jobs'] = rows;
      }
      notifyListeners();
      return;
    }
    await api.post('/jobs/$jobId/action',
        {'action': action, if (position != null) 'position': position});
    await refreshJobsAndDashboard();
  }

  Future<void> editJobPreset(
    String jobId,
    String preset, {
    Map<String, dynamic>? smartTuning,
  }) async {
    _requireControl();
    if (!{'smart', 'auto', '1080', '4k'}.contains(preset)) {
      throw const ApiFailure('Choose Smart, Auto, 1080p, or 4K.');
    }
    if (demoMode) {
      Map<String, dynamic>? row;
      for (final value in _list(jobs['jobs'])) {
        final candidate = _map(value);
        if ('${candidate['id'] ?? ''}' == jobId) {
          row = candidate;
          break;
        }
      }
      if (row != null) {
        row['preset'] = preset;
        row['queued_preset_name'] = preset == 'smart'
            ? 'Smart Preset'
            : preset == '4k'
                ? '4K preset'
                : preset == '1080'
                    ? '1080p preset'
                    : 'Automatic preset';
      }
      notifyListeners();
      return;
    }
    await api.post('/jobs/$jobId/preset', {
      'preset': preset,
      if (preset == 'smart' && smartTuning != null) 'smart_tuning': smartTuning,
    });
    await refreshJobsAndDashboard();
  }

  Future<void> clearJobs(String target) async {
    _requireControl();
    if (demoMode) {
      final terminal = {'done', 'error', 'canceled'};
      final rows = _list(jobs['jobs']);
      rows.removeWhere((row) {
        final status = '${_map(row)['status'] ?? ''}';
        return target == 'finished'
            ? terminal.contains(status)
            : status == 'queued';
      });
      jobs['jobs'] = rows;
      notifyListeners();
      return;
    }
    await api.post('/jobs/clear', {'target': target});
    await refreshJobsAndDashboard();
  }

  Future<Map<String, dynamic>> scanAudio({
    String path = '',
    String jobId = '',
    String jobType = 'audio_only',
    String audioPolicy = 'optimize_lossless',
    Map<String, dynamic>? operations,
  }) async {
    if (path.isEmpty && jobId.isEmpty) {
      throw const ApiFailure('Choose a media file or completed job first.');
    }
    if (demoMode) {
      return {
        'ok': true,
        'path': path.isNotEmpty ? path : '/media/Demo Movie-TSD.mkv',
        'inventory': {
          'duration_seconds': 7200,
          'audio_streams': [
            {
              'index': 1,
              'type_ordinal': 0,
              'language': 'eng',
              'title': 'Main audio',
              'codec': 'dts',
              'codec_label': 'DTS-HD MA',
              'channels': 8,
              'channel_layout': '7.1',
              'bitrate': 4800000,
              'sample_rate': 48000,
              'size_bytes': 4369051648,
              'lossless': true,
              'default': true,
            },
            {
              'index': 2,
              'type_ordinal': 1,
              'language': 'spa',
              'codec': 'ac3',
              'codec_label': 'AC3',
              'channels': 6,
              'channel_layout': '5.1',
              'bitrate': 640000,
              'sample_rate': 48000,
              'size_bytes': 547608330,
              'lossless': false,
            },
          ],
        },
        'operations': {
          'job_type': 'audio_only',
          'audio_policy': 'optimize_lossless',
          'replace_source': true,
          'audio_actions': [
            {
              'stream_index': 1,
              'audio_ordinal': 0,
              'language': 'eng',
              'source_codec_label': 'DTS-HD MA',
              'source_channels': 8,
              'source_size_bytes': 4369051648,
              'action': 'encode',
              'target_codec': 'aac',
              'bitrate_kbps': 1024,
              'channels': 8,
              'sample_rate': 48000,
              'gain_db': 0,
              'preserve_metadata': true,
              'allow_downmix': false,
            },
            {
              'stream_index': 2,
              'audio_ordinal': 1,
              'language': 'spa',
              'source_codec_label': 'AC3',
              'source_channels': 6,
              'source_size_bytes': 547608330,
              'action': 'copy',
              'target_codec': 'aac',
              'bitrate_kbps': 640,
              'channels': 6,
              'sample_rate': 48000,
              'gain_db': 0,
              'preserve_metadata': true,
              'allow_downmix': false,
            },
          ],
          'estimate': {
            'current_audio_bytes': 4916660000,
            'output_audio_bytes': 1322608330,
            'audio_savings_bytes': 3594051670,
            'audio_savings_percent': 73.1,
            'estimated': true,
          },
          'warnings': [
            'DTS-HD MA conversion is lossy; review before queueing.'
          ],
        },
      };
    }
    final safeOperations = operations == null
        ? null
        : <String, dynamic>{
            ...operations,
            'job_type': 'audio_only',
            'video_action': 'copy',
            'subtitle_action': 'copy',
          };
    return api.post(
      '/audio/scan',
      {
        if (path.isNotEmpty) 'src': path,
        if (jobId.isNotEmpty) 'job_id': jobId,
        'job_type': jobType,
        'audio_policy': audioPolicy,
        if (safeOperations != null) 'operations': safeOperations,
      },
      timeout: const Duration(minutes: 5),
    );
  }

  Future<Map<String, dynamic>> queueAudioOptimization(
    String completedJobId,
    Map<String, dynamic> operations, {
    String mode = 'next_available',
  }) async {
    _requireControl();
    if (completedJobId.isEmpty) {
      throw const ApiFailure('The completed job is missing its identifier.');
    }
    if (demoMode) return {'ok': true, 'job_id': 'demo-audio-only'};
    final safeOperations = <String, dynamic>{
      ...operations,
      'job_type': 'audio_only',
      'video_action': 'copy',
      'subtitle_action': 'copy',
    };
    final result = await api.post(
      '/jobs/$completedJobId/optimize-audio',
      {'operations': safeOperations, 'mode': mode},
      timeout: const Duration(minutes: 5),
    );
    await refreshJobsAndDashboard();
    return result;
  }

  Future<Map<String, dynamic>> queueAudioOptimizationPath(
    String path,
    Map<String, dynamic> operations, {
    String mode = 'next_available',
  }) async {
    _requireControl();
    if (path.isEmpty) {
      throw const ApiFailure('Choose a media file first.');
    }
    if (demoMode) return {'ok': true, 'job_id': 'demo-audio-only'};
    final safeOperations = <String, dynamic>{
      ...operations,
      'job_type': 'audio_only',
      'video_action': 'copy',
      'subtitle_action': 'copy',
    };
    final result = await api.post(
      '/audio/queue',
      {'src': path, 'operations': safeOperations, 'mode': mode},
      timeout: const Duration(minutes: 5),
    );
    await refreshJobsAndDashboard();
    return result;
  }

  Future<void> refreshJobsAndDashboard() async {
    if (demoMode) return;
    final values = await Future.wait([
      api.get('/jobs?limit=300', timeout: const Duration(seconds: 45)),
      api.get('/dashboard', timeout: const Duration(seconds: 45)),
    ]);
    jobs = values[0];
    dashboard = values[1];
    notifyListeners();
  }

  Future<void> setShowSecondaryUi(bool value) async {
    showSecondaryUi = value;
    await store.saveUiVisibility(showSecondaryUi: value);
    notifyListeners();
  }

  Future<void> setStatsForNerds(bool value) async {
    statsForNerds = value;
    await store.saveUiVisibility(statsForNerds: value);
    notifyListeners();
  }

  Future<void> nodeAction(
    String nodeId,
    String action, {
    String? name,
    int? hardwareTranscodeConcurrency,
  }) async {
    _requireControl();
    if (demoMode) {
      final rows = _list(nodes['nodes']);
      final index = rows.indexWhere(
        (value) => '${_map(value)['id'] ?? ''}' == nodeId,
      );
      if (index >= 0) {
        final row = _map(rows[index]);
        if (action == 'rename' && name != null) row['name'] = name.trim();
        if (action == 'capacity' && hardwareTranscodeConcurrency != null) {
          row['hardware_transcode_concurrency'] =
              hardwareTranscodeConcurrency.clamp(1, 8);
        }
        if (action == 'unlink' || action == 'forget') rows.removeAt(index);
      }
      notifyListeners();
      return;
    }
    await api.post('/nodes/$nodeId/action', {
      'action': action,
      if (name != null) 'name': name,
      if (hardwareTranscodeConcurrency != null)
        'hardware_transcode_concurrency': hardwareTranscodeConcurrency,
    });
    final values = await Future.wait([
      api.get('/nodes'),
      api.get('/jobs?limit=300', timeout: const Duration(seconds: 45)),
      api.get('/dashboard'),
    ]);
    nodes = values[0];
    jobs = values[1];
    dashboard = values[2];
    notifyListeners();
  }

  Future<void> refreshLibrary() async {
    _requireControl();
    if (demoMode) return;
    busy = true;
    notifyListeners();
    try {
      final value = await api.post('/library/refresh', {},
          timeout: const Duration(minutes: 3));
      library = _map(value['library']);
      dashboard = await api.get('/dashboard');
    } finally {
      busy = false;
      notifyListeners();
    }
  }

  Future<void> queuePaths(
    List<String> paths, {
    String preset = 'smart',
    String mode = 'local',
    String? nodeId,
    Map<String, dynamic>? smartTuning,
  }) async {
    _requireControl();
    if (paths.isEmpty) {
      throw const ApiFailure('No media files are available to queue.');
    }
    if (demoMode) return;
    await api.post(
        '/library/queue',
        {
          'paths': paths,
          'preset': preset,
          'mode': mode,
          if (nodeId != null && nodeId.isNotEmpty) 'node_id': nodeId,
          if (preset == 'smart' && smartTuning != null)
            'smart_tuning': smartTuning,
        },
        timeout: const Duration(minutes: 2));
    await refreshJobsAndDashboard();
  }

  Future<Map<String, dynamic>> planSizeWizard(
    String path, {
    bool smartStart = false,
    Map<String, dynamic>? options,
    String smartCandidateId = '',
  }) async {
    if (path.isEmpty) {
      throw const ApiFailure('No media file is available for Size Wizard.');
    }
    if (demoMode) {
      final demoOptions = <String, dynamic>{
        'target_size_auto': true,
        'target_size_value': 1.0,
        'target_size_unit': 'GB',
        'resolution_mode': 'keep',
        'video_codec': 'h265',
        'encoder_family': 'qsv',
        'bit_depth': '10',
        'quality': 'balanced',
        'encoder_speed': 'auto',
        'audio_mode': 'copy',
        'audio_tracks': 'all',
        'subtitle_mode': 'all',
        'framerate_mode': 'same',
        ...?options,
      };
      final qualityFactor = const <String, double>{
            'high': 1.28,
            'balanced': 1.0,
            'small': .74,
          }['${demoOptions['quality']}'] ??
          1.0;
      final codecFactor = const <String, double>{
            'h264': 1.24,
            'h265': 1.0,
            'av1': .84,
          }['${demoOptions['video_codec']}'] ??
          1.0;
      final resolutionMode = '${demoOptions['resolution_mode']}';
      final resolutionFactor = const <String, double>{
            '720': .24,
            '1080': 1.0,
            // The demo source is 1080p; larger caps must never upscale it.
            '1440': 1.0,
            '2160': 1.0,
          }[resolutionMode] ??
          1.0;
      final automaticMb =
          (3180.0 * qualityFactor * codecFactor * resolutionFactor)
              .clamp(180.0, 7864.0)
              .toDouble();
      final manualValue =
          (demoOptions['target_size_value'] as num?)?.toDouble() ?? 1.0;
      final targetMb = demoOptions['target_size_auto'] == false
          ? manualValue *
              ('${demoOptions['target_size_unit']}' == 'GB' ? 1024.0 : 1.0)
          : automaticMb;
      demoOptions['target_size_value'] =
          double.parse(targetMb.toStringAsFixed(1));
      demoOptions['target_size_unit'] = 'MB';
      final encoderKey =
          '${demoOptions['encoder_family']}:${demoOptions['video_codec']}:${demoOptions['bit_depth']}';
      final demoEncoder = const <String, String>{
            'qsv:h264:8': 'qsv_h264',
            'qsv:h265:8': 'qsv_h265',
            'qsv:h265:10': 'qsv_h265_10bit',
            'qsv:av1:8': 'qsv_av1',
            'qsv:av1:10': 'qsv_av1_10bit',
            'nvenc:h264:8': 'nvenc_h264',
            'nvenc:h265:8': 'nvenc_h265',
            'nvenc:h265:10': 'nvenc_h265_10bit',
            'nvenc:av1:8': 'nvenc_av1',
            'nvenc:av1:10': 'nvenc_av1_10bit',
            'software:h264:8': 'x264',
            'software:h265:8': 'x265',
            'software:h265:10': 'x265_10bit',
            'software:av1:8': 'svt_av1',
            'software:av1:10': 'svt_av1_10bit',
          }[encoderKey] ??
          'x265_10bit';
      final familyLabel = const <String, String>{
            'qsv': 'Intel QSV',
            'nvenc': 'NVIDIA NVENC',
            'software': 'CPU',
          }['${demoOptions['encoder_family']}'] ??
          'CPU';
      final codecLabel = const <String, String>{
            'h264': 'H.264',
            'h265': 'H.265',
            'av1': 'AV1',
          }['${demoOptions['video_codec']}'] ??
          'H.265';
      return <String, dynamic>{
        'ok': true,
        'smart_start': smartStart,
        'smart_candidate_id': smartStart ? 'balanced' : 'manual',
        'smart_candidate_name':
            smartStart ? 'Learned balanced plan' : 'Custom Size Wizard plan',
        'learned_defaults': {'sample_count': 4, 'confidence': .82},
        'learning': {'feedback_count': 4, 'automation_ready': true},
        'plan': {
          'src': path,
          'preset': '1080',
          'probe': {
            'width': 1920,
            'height': 1080,
            'fps': 23.976,
            'duration_sec': 6480,
            'source_size_bytes': 8589934592,
            'is_hdr': false,
          },
          'options': demoOptions,
          'inputs': {'target_mb': double.parse(targetMb.toStringAsFixed(1))},
          'estimates': {
            'encoder': demoEncoder,
            'encoder_label':
                '$familyLabel $codecLabel ${demoOptions['bit_depth']}-bit',
            'video_bitrate_kbps': double.parse(
                ((targetMb * 8 * 1024 * 1024 / 6480) / 1000)
                    .toStringAsFixed(1)),
            'output_resolution': {'width': 1920, 'height': 1080},
            'eta_human': '28 minutes',
            'quality_label': 'Good',
            'estimated_output_mb': double.parse(targetMb.toStringAsFixed(1)),
            'auto_target': {
              'mode': demoOptions['target_size_auto'] == false
                  ? 'manual'
                  : 'source_aware',
              'summary': demoOptions['target_size_auto'] == false
                  ? 'Manual target size.'
                  : 'Calculated for this source from title runtime, resolution, codec, quality, and approved similar plans.',
              'learned_sample_count': 4,
            },
          },
        },
      };
    }
    return api.post(
      '/size_wizard/plan',
      {
        ...?options,
        'src': path,
        'preset': '${options?['preset'] ?? 'auto'}',
        'smart_start': smartStart,
        if (smartCandidateId.isNotEmpty) 'smart_candidate_id': smartCandidateId,
      },
      timeout: const Duration(minutes: 2),
    );
  }

  Future<Map<String, dynamic>> queueSizeWizard(
    String path,
    Map<String, dynamic> options, {
    String smartCandidateId = 'manual',
    String mode = 'local',
    String? nodeId,
  }) async {
    _requireControl();
    if (path.isEmpty) {
      throw const ApiFailure('No media file is available for Size Wizard.');
    }
    if (demoMode) {
      final learning = _map(smartPresets['learning']);
      learning['feedback_count'] =
          ((learning['feedback_count'] as num?)?.toInt() ?? 0) + 1;
      smartPresets['learning'] = learning;
      notifyListeners();
      return {
        'ok': true,
        'job_id': 'demo-size-wizard',
        'learning_recorded': true,
        'learning': learning,
        'dispatch_mode': mode,
        if (nodeId != null && nodeId.isNotEmpty) 'node_id': nodeId,
      };
    }
    final value = await api.post(
      '/size_wizard/queue',
      {
        ...options,
        'src': path,
        'preset': '${options['preset'] ?? 'auto'}',
        'smart_candidate_id': smartCandidateId,
        'mode': mode,
        if (nodeId != null && nodeId.isNotEmpty) 'node_id': nodeId,
      },
      timeout: const Duration(minutes: 2),
    );
    final refreshed = await Future.wait([
      api.get('/jobs?limit=300', timeout: const Duration(seconds: 45)),
      api.get('/dashboard'),
      api.get('/smart_presets'),
    ]);
    jobs = refreshed[0];
    dashboard = refreshed[1];
    smartPresets = refreshed[2];
    notifyListeners();
    return value;
  }

  Future<Map<String, dynamic>> generateLibraryPreview(
    String path, {
    Map<String, dynamic>? smartTuning,
    ValueChanged<Map<String, dynamic>>? onProgress,
  }) async {
    _requireControl();
    if (path.isEmpty) {
      throw const ApiFailure('No media file is available to preview.');
    }
    if (demoMode) {
      final preview = <String, dynamic>{
        'state': 'done',
        'progress': 100,
        'message': 'Demo Smart preview ready.',
        'result': {
          'encoder_label': 'Smart H.265 10-bit',
          'out_width': 3840,
          'out_height': 2160,
        },
      };
      onProgress?.call(preview);
      return preview;
    }

    final started = await api.post(
      '/library/preview',
      {
        'src': path,
        'smart_tuning': smartTuning ?? <String, dynamic>{},
      },
      timeout: const Duration(minutes: 2),
    );
    final previewId = '${_map(started['preview'])['preview_id'] ?? ''}';
    if (previewId.isEmpty) {
      throw const ApiFailure('The server did not return a preview id.');
    }

    for (var attempt = 0; attempt < 240; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 1400));
      final value = await api.get(
        '/library/preview/$previewId',
        timeout: const Duration(seconds: 30),
      );
      final preview = _map(value['preview']);
      onProgress?.call(preview);
      final state = '${preview['state'] ?? ''}';
      if (state == 'done') return preview;
      if (state == 'error' || state == 'canceled' || state == 'expired') {
        throw ApiFailure(
          '${preview['error'] ?? preview['message'] ?? 'Preview failed.'}',
        );
      }
    }
    throw const ApiFailure('The Smart preview took too long to finish.');
  }

  Future<void> trackShow(Map<String, dynamic> show, bool tracked) async {
    _requireControl();
    show['tracked'] = tracked;
    notifyListeners();
    if (demoMode) return;
    final files = _list(show['files'])
        .map((row) => '${_map(row)['path'] ?? ''}')
        .where((path) => path.isNotEmpty)
        .toList();
    await api.post('/library/tracked_show', {
      'show_id': show['id'],
      'title': show['title'],
      'year': show['year'],
      'tmdb_id': show['tmdb_id'],
      'tvmaze_id': show['tvmaze_id'],
      'poster_url': show['poster_url'],
      'paths': files,
      'tracked': tracked,
      'monitor_releases': show['monitor_releases'] != false,
      'auto_queue': show['auto_queue_downloads'] != false,
    });
    if (!demoMode) {
      final value = await api.get('/calendar?days=180');
      calendar = _map(value['calendar']);
      notifyListeners();
    }
  }

  Future<void> saveAutomation(Map<String, dynamic> updates) async {
    _requireControl();
    if (demoMode) {
      _map(automation['settings']).addAll(updates);
      notifyListeners();
      return;
    }
    automation = await api.post('/automation', {'action': 'save', ...updates});
    dashboard = await api.get('/dashboard');
    notifyListeners();
  }

  Future<void> runAutopilot() async {
    _requireControl();
    if (demoMode) return;
    busy = true;
    notifyListeners();
    try {
      automation = await api.post('/automation', {'action': 'run'},
          timeout: const Duration(minutes: 3));
      dashboard = await api.get('/dashboard');
    } finally {
      busy = false;
      notifyListeners();
    }
  }

  Future<void> saveSmartProfile(Map<String, dynamic> profile) async {
    _requireControl();
    if (demoMode) {
      smartPresets['profile'] = profile;
      notifyListeners();
      return;
    }
    smartPresets = await api.post('/smart_presets', {'profile': profile});
    notifyListeners();
  }

  Future<void> saveOperationsSettings(Map<String, dynamic> updates) async {
    _requireControl();
    if (demoMode) {
      operations.addAll(updates);
      final summary = _map(jobs['summary']);
      final dashboardSummary = _map(_map(dashboard['queue'])['summary']);
      if (updates['hardware_transcode_concurrency'] != null) {
        summary['hardware_transcode_concurrency'] =
            updates['hardware_transcode_concurrency'];
        dashboardSummary['hardware_transcode_concurrency'] =
            updates['hardware_transcode_concurrency'];
      }
      notifyListeners();
      return;
    }
    if (!serverSupportsOperationsSettings) {
      throw const ApiFailure(
        'Update the TSD server to version 3.15 or newer before changing encoder settings from ByteSqueeze.',
      );
    }
    try {
      final value = await api.post('/operations', updates);
      operations = _map(value['settings']);
      await refreshJobsAndDashboard();
      notifyListeners();
    } on ApiFailure catch (failure) {
      if (_unsupportedOperationsEndpoint(failure)) {
        serverSupportsOperationsSettings = false;
        notifyListeners();
        throw const ApiFailure(
          'Update the TSD server to version 3.15 or newer before changing encoder settings from ByteSqueeze.',
        );
      }
      rethrow;
    }
  }

  Future<void> startAutopilotReview({bool next = false}) async {
    _requireControl();
    if (demoMode) return;
    final value = await api.post('/autopilot/review', {'next': next});
    autopilotReview = _map(value['review']);
    notifyListeners();
    _pollAutopilotReview();
  }

  Future<void> refreshAutopilotReview() async {
    if (demoMode) return;
    final value = await api.get('/autopilot/review');
    autopilotReview = _map(value['review']);
    notifyListeners();
  }

  Future<void> submitAutopilotReview(String verdict, String reason) async {
    _requireControl();
    if (demoMode) return;
    final value = await api.post('/autopilot/review/feedback', {
      'verdict': verdict,
      'reason': reason,
    });
    autopilotReview = _map(value['review']);
    smartPresets = await api.get('/smart_presets');
    automation = await api.get('/automation');
    notifyListeners();
  }

  Future<void> submitCompletedEncodeFeedback(
      String jobId, String verdict, String reason) async {
    _requireControl();
    if (demoMode) return;
    await api.post('/autopilot/completed/$jobId/feedback', {
      'verdict': verdict,
      'reason': reason,
    });
    automation = await api.get('/automation');
    smartPresets = await api.get('/smart_presets');
    notifyListeners();
  }

  Future<void> setAutopilotTourCompleted(bool completed) async {
    _requireControl();
    if (demoMode) {
      final status = _map(automation['status']);
      final onboarding = _map(status['onboarding']);
      onboarding['tour_completed'] = completed;
      status['onboarding'] = onboarding;
      automation['status'] = status;
      notifyListeners();
      return;
    }
    await api.post('/autopilot/onboarding', {'completed': completed});
    automation = await api.get('/automation');
    notifyListeners();
  }

  Future<void> _pollAutopilotReview() async {
    for (var attempt = 0; attempt < 240; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 1400));
      if (demoMode || session == null) return;
      try {
        final value = await api.get('/autopilot/review');
        autopilotReview = _map(value['review']);
        notifyListeners();
        final preview = _map(autopilotReview['preview']);
        final state = '${preview['state'] ?? ''}';
        if (state == 'done' || state == 'error' || state == 'expired') return;
      } catch (_) {
        return;
      }
    }
  }

  Future<void> updateServerAddresses(String primary, String fallback) async {
    if (demoMode) return;
    session =
        await api.updateAddresses(baseUrl: primary, fallbackBaseUrl: fallback);
    notifyListeners();
    await refreshAll();
  }

  void _requireControl() {
    if (!canControl) {
      throw const ApiFailure('This device was paired with read-only access.');
    }
  }

  String _message(Object failure) =>
      failure is ApiFailure ? failure.message : '$failure';

  static bool _unsupportedOperationsEndpoint(ApiFailure failure) =>
      failure.statusCode == 404 || failure.statusCode == 405;

  static Map<String, dynamic> _map(dynamic value) {
    return value is Map<String, dynamic> ? value : <String, dynamic>{};
  }

  static List<dynamic> _list(dynamic value) =>
      value is List ? value : <dynamic>[];
}
