import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../app_meta.dart';
import '../theme.dart';

Map<String, dynamic> asMap(dynamic value) =>
    value is Map<String, dynamic> ? value : <String, dynamic>{};
List<dynamic> asList(dynamic value) => value is List ? value : <dynamic>[];

int summaryCount(Map<String, dynamic> summary, String key) {
  final counts = asMap(summary['counts']);
  final value = summary[key] ?? counts[key];
  if (value is num) return value.toInt();
  if (key == 'queued' && summary['queued_count'] is num) {
    return (summary['queued_count'] as num).toInt();
  }
  return int.tryParse('$value') ?? 0;
}

String fileName(dynamic path) {
  final value = '${path ?? ''}'.replaceAll('\\', '/');
  return value.split('/').where((part) => part.isNotEmpty).lastOrNull ??
      'Unknown media';
}

String formatBytes(dynamic value, {int decimals = 1}) {
  final bytes =
      value is num ? value.toDouble() : double.tryParse('$value') ?? 0;
  if (bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  final exponent = math.min(
    units.length - 1,
    (math.log(bytes) / math.log(1024)).floor(),
  );
  final amount = bytes / math.pow(1024, exponent);
  return '${amount.toStringAsFixed(exponent == 0 ? 0 : decimals)} ${units[exponent]}';
}

String formatDuration(dynamic seconds) {
  final total =
      seconds is num ? seconds.round() : int.tryParse('$seconds') ?? 0;
  if (total <= 0) return '—';
  final hours = total ~/ 3600;
  final minutes = (total % 3600) ~/ 60;
  if (hours > 0) return '${hours}h ${minutes}m';
  return '${math.max(1, minutes)}m';
}

String relativeTime(dynamic timestamp) {
  final seconds = timestamp is num
      ? timestamp.toDouble()
      : double.tryParse('$timestamp') ?? 0;
  if (seconds <= 0) return 'No recent activity';
  final difference = DateTime.now().difference(
    DateTime.fromMillisecondsSinceEpoch((seconds * 1000).round()),
  );
  if (difference.inMinutes < 1) return 'Just now';
  if (difference.inHours < 1) return '${difference.inMinutes}m ago';
  if (difference.inDays < 1) return '${difference.inHours}h ago';
  return '${difference.inDays}d ago';
}

bool jobAudioWasProcessed(Map<String, dynamic> job) {
  if (job['audio_processed'] is bool) return job['audio_processed'] == true;
  final operations = asMap(job['operations']);
  final jobType =
      '${job['job_type'] ?? operations['job_type'] ?? ''}'.trim().toLowerCase();
  final policy = '${operations['audio_policy'] ?? 'preserve'}'.toLowerCase();
  final changedTrack = asList(operations['audio_actions']).map(asMap).any(
        (row) => {'encode', 'remove'}
            .contains('${row['action'] ?? 'copy'}'.toLowerCase()),
      );
  return jobType == 'audio_only' || policy != 'preserve' || changedTrack;
}

String jobVideoProcessingLabel(Map<String, dynamic> job) {
  final supplied = '${job['video_processing_label'] ?? ''}'.trim();
  if (supplied.isNotEmpty) return supplied;
  final operations = asMap(job['operations']);
  final jobType =
      '${job['job_type'] ?? operations['job_type'] ?? ''}'.trim().toLowerCase();
  return jobType == 'audio_only' || operations['video_action'] == 'copy'
      ? 'Video copied'
      : 'Video encoded';
}

String jobAudioProcessingLabel(Map<String, dynamic> job) {
  final supplied = '${job['audio_processing_label'] ?? ''}'.trim();
  if (supplied.isNotEmpty) return supplied;
  final actions = asList(asMap(job['operations'])['audio_actions']).map(asMap);
  if (actions.any((row) => '${row['action']}' == 'encode')) {
    return 'Audio encoded';
  }
  if (actions.any((row) => '${row['action']}' == 'remove')) {
    return 'Audio tracks changed';
  }
  return jobAudioWasProcessed(job) ? 'Audio optimized' : 'Audio copied';
}

Color statusColor(String status) {
  switch (status.toLowerCase()) {
    case 'running':
    case 'online':
    case 'done':
    case 'success':
      return ByteSqueezeColors.mint;
    case 'queued':
    case 'waiting_to_upload':
      return ByteSqueezeColors.cyan;
    case 'error':
    case 'offline':
    case 'canceled':
      return ByteSqueezeColors.danger;
    default:
      return ByteSqueezeColors.amber;
  }
}

class PageInsets extends StatelessWidget {
  const PageInsets({super.key, required this.child, this.top = 8, this.bottom});

  final Widget child;
  final double top;
  final double? bottom;

  @override
  Widget build(BuildContext context) {
    final classic =
        Theme.of(context).colorScheme.primary == ByteSqueezeColors.blue;
    final narrow = MediaQuery.sizeOf(context).width < 880;
    return Center(
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 1280),
        child: Padding(
          padding: EdgeInsets.fromLTRB(
            MediaQuery.sizeOf(context).width < 600 ? 14 : 24,
            top,
            MediaQuery.sizeOf(context).width < 600 ? 14 : 24,
            bottom ?? (classic ? (narrow ? 184 : 104) : (narrow ? 24 : 40)),
          ),
          child: child,
        ),
      ),
    );
  }
}

class SurfaceCard extends StatelessWidget {
  const SurfaceCard({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(18),
    this.onTap,
    this.gradient,
    this.borderColor,
  });

  final Widget child;
  final EdgeInsetsGeometry padding;
  final VoidCallback? onTap;
  final Gradient? gradient;
  final Color? borderColor;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final classic = theme.colorScheme.primary == ByteSqueezeColors.blue;
    final compact = theme.visualDensity == VisualDensity.compact;
    final radius = BorderRadius.circular(classic ? 17 : (compact ? 12 : 14));
    return Material(
      color: Colors.transparent,
      borderRadius: radius,
      clipBehavior: Clip.antiAlias,
      child: Ink(
        decoration: BoxDecoration(
          color: gradient == null ? ByteSqueezeColors.surface : null,
          gradient: gradient,
          borderRadius: radius,
          border: Border.all(
            color: borderColor ?? ByteSqueezeColors.line,
            width: .7,
          ),
          boxShadow: classic
              ? const [
                  BoxShadow(
                    color: Color(0x25000000),
                    blurRadius: 16,
                    offset: Offset(0, 8),
                  ),
                ]
              : const [],
        ),
        child: InkWell(
          onTap: onTap,
          borderRadius: radius,
          child: Padding(padding: padding, child: child),
        ),
      ),
    );
  }
}

class SectionHeader extends StatelessWidget {
  const SectionHeader({
    super.key,
    required this.title,
    this.subtitle,
    this.trailing,
  });

  final String title;
  final String? subtitle;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 16, bottom: 9),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: Theme.of(context).textTheme.titleLarge),
                if (subtitle != null) ...[
                  const SizedBox(height: 3),
                  Text(
                    subtitle!,
                    style: const TextStyle(
                      color: ByteSqueezeColors.muted,
                      fontSize: 13,
                    ),
                  ),
                ],
              ],
            ),
          ),
          if (trailing != null) trailing!,
        ],
      ),
    );
  }
}

class CompactPageHeader extends StatelessWidget {
  const CompactPageHeader({
    super.key,
    required this.title,
    this.summary,
    this.status,
    this.statusColor = ByteSqueezeColors.mint,
    this.trailing,
  });

  final String title;
  final String? summary;
  final String? status;
  final Color statusColor;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.center,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Flexible(
                      child: Text(
                        title,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: Theme.of(context).textTheme.headlineLarge,
                      ),
                    ),
                    if (status != null) ...[
                      const SizedBox(width: 9),
                      Icon(Icons.circle, color: statusColor, size: 8),
                      const SizedBox(width: 5),
                      Flexible(
                        child: Text(
                          status!,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: ByteSqueezeColors.softInk,
                            fontSize: 12,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ),
                    ],
                  ],
                ),
                if (summary != null) ...[
                  const SizedBox(height: 3),
                  Text(
                    summary!,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: ByteSqueezeColors.muted,
                      fontSize: 12.5,
                    ),
                  ),
                ],
              ],
            ),
          ),
          if (trailing != null) ...[const SizedBox(width: 10), trailing!],
        ],
      ),
    );
  }
}

class StatusPill extends StatelessWidget {
  const StatusPill({
    super.key,
    required this.label,
    this.color = ByteSqueezeColors.cyan,
    this.icon,
  });

  final String label;
  final Color color;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        color: color.withValues(alpha: .12),
        border: Border.all(color: color.withValues(alpha: .34)),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (icon != null) ...[
              Icon(icon, color: color, size: 14),
              const SizedBox(width: 5),
            ],
            Text(
              label,
              style: TextStyle(
                color: color,
                fontSize: 12,
                fontWeight: FontWeight.w700,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class MediaOperationTag extends StatelessWidget {
  const MediaOperationTag({
    super.key,
    required this.label,
    required this.icon,
    this.color = ByteSqueezeColors.softInk,
  });

  final String label;
  final IconData icon;
  final Color color;

  @override
  Widget build(BuildContext context) => DecoratedBox(
        decoration: BoxDecoration(
          color: color.withValues(alpha: .08),
          borderRadius: BorderRadius.circular(999),
          border: Border.all(color: color.withValues(alpha: .22)),
        ),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 13, color: color),
              const SizedBox(width: 5),
              Text(
                label,
                style: TextStyle(
                  color: color,
                  fontSize: 10.5,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ],
          ),
        ),
      );
}

class OperationsDock extends StatelessWidget {
  const OperationsDock({
    super.key,
    required this.summary,
    required this.activeJobs,
    required this.paused,
    required this.onTap,
  });

  final Map<String, dynamic> summary;
  final List<dynamic> activeJobs;
  final bool paused;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final running = activeJobs
        .map(asMap)
        .where((job) => '${job['status'] ?? ''}'.toLowerCase() == 'running')
        .toList();
    final runningCount = summaryCount(summary, 'running');
    final queuedCount = summaryCount(summary, 'queued');
    final current = running.isEmpty ? <String, dynamic>{} : running.first;
    final progress = ((current['progress'] as num?)?.toDouble() ?? 0)
        .clamp(0, 100)
        .toDouble();
    final state = current.isNotEmpty
        ? fileName(current['src'])
        : paused
            ? 'Queue paused'
            : '$runningCount encoding now';
    final encoder = '${current['encoder'] ?? current['preset'] ?? 'Smart'}';
    final worker =
        '${current['node_name'] ?? (current['is_worker_job'] == true ? 'Linked worker' : 'Main controller')}';
    final fps = (current['fps'] as num?)?.toDouble();
    final eta = formatDuration(current['eta_seconds']);
    final detail = current.isNotEmpty
        ? '$encoder · $worker${fps != null && fps > 0 ? ' · ${fps.toStringAsFixed(0)} FPS' : ''}${eta != '—' ? ' · $eta left' : ''}'
        : '$queuedCount waiting';
    final color = paused
        ? ByteSqueezeColors.amber
        : runningCount > 0
            ? ByteSqueezeColors.cyan
            : ByteSqueezeColors.mint;

    return Material(
      color: const Color(0xF50B1119),
      borderRadius: BorderRadius.circular(12),
      clipBehavior: Clip.antiAlias,
      elevation: 0,
      child: InkWell(
        onTap: onTap,
        child: DecoratedBox(
          decoration: BoxDecoration(
            border: Border.all(color: ByteSqueezeColors.line),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(10, 8, 10, 7),
            child: Row(
              children: [
                DecoratedBox(
                  decoration: BoxDecoration(
                    color: color.withValues(alpha: .12),
                    borderRadius: BorderRadius.circular(9),
                    border: Border.all(color: color.withValues(alpha: .25)),
                  ),
                  child: Padding(
                    padding: const EdgeInsets.all(8),
                    child: Icon(
                      paused ? Icons.pause_rounded : Icons.graphic_eq_rounded,
                      color: color,
                      size: 19,
                    ),
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        state,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          fontWeight: FontWeight.w800,
                          fontSize: 12.5,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        detail,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: ByteSqueezeColors.muted,
                          fontSize: 10.5,
                        ),
                      ),
                      if (runningCount > 0) ...[
                        const SizedBox(height: 7),
                        ClipRRect(
                          borderRadius: BorderRadius.circular(999),
                          child: LinearProgressIndicator(
                            value: progress / 100,
                            minHeight: 3,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(width: 10),
                Text(
                  '${progress.toStringAsFixed(0)}%',
                  style: const TextStyle(
                    fontWeight: FontWeight.w800,
                    fontSize: 13,
                  ),
                ),
                const SizedBox(width: 3),
                const Icon(
                  Icons.chevron_right_rounded,
                  color: ByteSqueezeColors.muted,
                  size: 20,
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class MetricCard extends StatelessWidget {
  const MetricCard({
    super.key,
    required this.label,
    required this.value,
    required this.icon,
    this.color = ByteSqueezeColors.cyan,
    this.detail,
  });

  final String label;
  final String value;
  final IconData icon;
  final Color color;
  final String? detail;

  @override
  Widget build(BuildContext context) {
    return SurfaceCard(
      padding: const EdgeInsets.all(13),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(icon, color: color, size: 18),
              const Spacer(),
              if (detail != null)
                Flexible(
                  child: Text(
                    detail!,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    textAlign: TextAlign.right,
                    style: const TextStyle(
                      color: ByteSqueezeColors.muted,
                      fontSize: 10.5,
                    ),
                  ),
                ),
            ],
          ),
          const SizedBox(height: 13),
          Text(
            value,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: Theme.of(context).textTheme.headlineSmall,
          ),
          const SizedBox(height: 3),
          Text(
            label,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              color: ByteSqueezeColors.muted,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }
}

class BrandMark extends StatelessWidget {
  const BrandMark({super.key, this.size = 38, this.showName = true});

  final double size;
  final bool showName;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        ClipRRect(
          borderRadius: BorderRadius.circular(size * .24),
          child: Image.asset(
            'assets/branding/bytesqueeze_icon.png',
            width: size,
            height: size,
          ),
        ),
        if (showName) ...[
          const SizedBox(width: 10),
          Text('ByteSqueeze', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(width: 8),
          const DecoratedBox(
            decoration: BoxDecoration(
              color: Color(0x18A78BFA),
              border: Border.fromBorderSide(
                BorderSide(color: Color(0x44A78BFA)),
              ),
              borderRadius: BorderRadius.all(Radius.circular(999)),
            ),
            child: Padding(
              padding: EdgeInsets.symmetric(horizontal: 7, vertical: 4),
              child: Text(
                appReleaseLabel,
                style: TextStyle(
                  color: Color(0xFFD8CCFF),
                  fontSize: 9,
                  fontWeight: FontWeight.w800,
                  letterSpacing: .8,
                ),
              ),
            ),
          ),
        ],
      ],
    );
  }
}

class PosterArt extends StatelessWidget {
  const PosterArt({super.key, required this.item, this.borderRadius = 14});

  final Map<String, dynamic> item;
  final double borderRadius;

  @override
  Widget build(BuildContext context) {
    final url = '${item['poster_url'] ?? ''}'.trim();
    final title = '${item['title'] ?? 'Media'}';
    final identity = '${item['id'] ?? ''}|${item['path'] ?? ''}|$title|$url';
    final hue = ((item['demo_hue'] as num?)?.toDouble() ??
        (title.hashCode.abs() % 360).toDouble());
    final first = HSVColor.fromAHSV(1, hue, .78, .88).toColor();
    final second = HSVColor.fromAHSV(1, (hue + 42) % 360, .82, .32).toColor();
    final fallback = DecoratedBox(
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [first, second, ByteSqueezeColors.navy],
        ),
      ),
      child: Stack(
        fit: StackFit.expand,
        children: [
          Positioned(
            right: -28,
            top: -24,
            child: Icon(
              Icons.movie_filter_rounded,
              size: 120,
              color: Colors.white.withValues(alpha: .10),
            ),
          ),
          Align(
            alignment: Alignment.bottomLeft,
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Text(
                title,
                maxLines: 4,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.w800,
                  height: 1.05,
                  shadows: [Shadow(blurRadius: 12, color: Colors.black)],
                ),
              ),
            ),
          ),
        ],
      ),
    );
    return ClipRRect(
      borderRadius: BorderRadius.circular(borderRadius),
      child: url.isEmpty
          ? fallback
          : Image.network(
              url,
              key: ValueKey(identity),
              fit: BoxFit.cover,
              errorBuilder: (_, __, ___) => fallback,
              loadingBuilder: (context, child, progress) => progress == null
                  ? child
                  : DecoratedBox(
                      decoration: const BoxDecoration(
                        color: ByteSqueezeColors.surface,
                      ),
                      child: Center(
                        child: SizedBox(
                          width: 24,
                          height: 24,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            value: progress.expectedTotalBytes == null
                                ? null
                                : progress.cumulativeBytesLoaded /
                                    progress.expectedTotalBytes!,
                          ),
                        ),
                      ),
                    ),
            ),
    );
  }
}

class EmptyState extends StatelessWidget {
  const EmptyState({
    super.key,
    required this.icon,
    required this.title,
    required this.message,
    this.action,
  });

  final IconData icon;
  final String title;
  final String message;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 30, vertical: 70),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 54, color: ByteSqueezeColors.muted),
            const SizedBox(height: 18),
            Text(
              title,
              style: Theme.of(context).textTheme.titleLarge,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 8),
            Text(
              message,
              style: const TextStyle(color: ByteSqueezeColors.muted),
              textAlign: TextAlign.center,
            ),
            if (action != null) ...[const SizedBox(height: 20), action!],
          ],
        ),
      ),
    );
  }
}

extension _LastOrNull<T> on Iterable<T> {
  T? get lastOrNull {
    final iterator = this.iterator;
    if (!iterator.moveNext()) return null;
    var value = iterator.current;
    while (iterator.moveNext()) {
      value = iterator.current;
    }
    return value;
  }
}
