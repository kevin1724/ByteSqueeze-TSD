import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../app_controller.dart';
import '../theme.dart';
import '../widgets/common.dart';

class DashboardScreen extends StatelessWidget {
  const DashboardScreen({super.key, required this.controller});

  final AppController controller;

  @override
  Widget build(BuildContext context) {
    final queue = asMap(controller.dashboard['queue']);
    final summary = asMap(queue['summary']);
    final library = asMap(controller.dashboard['library']);
    final nodes = asMap(controller.dashboard['nodes']);
    final dashboardStorage = asMap(controller.dashboard['storage']);
    final storageSummary = asMap(controller.storage['summary']);
    final storage = storageSummary.isEmpty ? dashboardStorage : storageSummary;
    final dashboardAnalytics = asMap(dashboardStorage['analytics']);
    final storageAnalytics = asMap(controller.storage['analytics']);
    final analytics =
        storageAnalytics.isEmpty ? dashboardAnalytics : storageAnalytics;
    final activeJobs = asList(controller.dashboard['active_jobs']);
    final events = asList(controller.dashboard['events']);
    final automationWrap = asMap(controller.dashboard['automation']);
    final autopilot = asMap(automationWrap['autopilot']);
    final paused = queue['paused'] == true;
    final runningJobs = activeJobs
        .map(asMap)
        .where((job) => '${job['status'] ?? ''}'.toLowerCase() == 'running')
        .toList();
    final online = (nodes['online'] as num?)?.toInt() ?? 0;
    final paired = (nodes['paired'] as num?)?.toInt() ?? 0;
    final savedBytes = storage['saved_bytes'] ?? summary['saved_bytes'] ?? 0;
    final completed =
        (storage['count'] as num?)?.toInt() ?? summaryCount(summary, 'done');

    return RefreshIndicator(
      onRefresh: controller.refreshAll,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        children: [
          PageInsets(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                CompactPageHeader(
                  title: 'Overview',
                  status: paused ? 'Queue paused' : 'Online · $online workers',
                  statusColor:
                      paused ? ByteSqueezeColors.amber : ByteSqueezeColors.mint,
                  summary: paired > 0
                      ? '$online/$paired linked workers available'
                      : 'Main controller ready',
                  trailing: controller.showSecondaryUi
                      ? IconButton(
                          tooltip: 'Refresh everything',
                          onPressed:
                              controller.busy ? null : controller.refreshAll,
                          icon: controller.busy
                              ? const SizedBox(
                                  width: 18,
                                  height: 18,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                  ),
                                )
                              : const Icon(Icons.refresh_rounded),
                        )
                      : null,
                ),
                LayoutBuilder(
                  builder: (context, constraints) {
                    final columns = constraints.maxWidth >= 980 ? 4 : 2;
                    final ratio = constraints.maxWidth >= 980
                        ? 1.7
                        : (constraints.maxWidth < 380 ? 1.25 : 1.42);
                    return GridView.count(
                      crossAxisCount: columns,
                      shrinkWrap: true,
                      physics: const NeverScrollableScrollPhysics(),
                      mainAxisSpacing: 9,
                      crossAxisSpacing: 9,
                      childAspectRatio: ratio,
                      children: [
                        MetricCard(
                          label: 'Running',
                          value: '${summaryCount(summary, 'running')}',
                          detail: '${summaryCount(summary, 'queued')} queued',
                          icon: Icons.graphic_eq_rounded,
                        ),
                        MetricCard(
                          label: 'Queue',
                          value: '${summaryCount(summary, 'queued')}',
                          detail: '$completed completed',
                          icon: Icons.queue_play_next_rounded,
                          color: ByteSqueezeColors.blue,
                        ),
                        MetricCard(
                          label: 'Space saved',
                          value: formatBytes(savedBytes),
                          detail: '$completed encodes',
                          icon: Icons.savings_outlined,
                          color: ByteSqueezeColors.mint,
                        ),
                        MetricCard(
                          label: 'Library',
                          value:
                              '${(library['movies'] as num?)?.toInt() ?? 0} movies',
                          detail:
                              '${(library['shows'] as num?)?.toInt() ?? 0} shows',
                          icon: Icons.video_library_rounded,
                          color: ByteSqueezeColors.amber,
                        ),
                      ],
                    );
                  },
                ),
                SectionHeader(
                  title: runningJobs.isEmpty ? 'Encoders' : 'Encoding now',
                  trailing: TextButton(
                    onPressed: () => controller.selectTab(2),
                    child: const Text('Open queue'),
                  ),
                ),
                if (runningJobs.isEmpty)
                  const _ReadyRow()
                else
                  ...runningJobs.take(4).map(
                        (job) => Padding(
                          padding: const EdgeInsets.only(bottom: 9),
                          child: _ActiveJobCard(job: job),
                        ),
                      ),
                const SectionHeader(
                  title: 'Storage impact',
                  subtitle: 'Savings, output efficiency, and node contribution',
                ),
                _StorageImpactCard(summary: storage, analytics: analytics),
                const SectionHeader(title: 'Achievements'),
                _AchievementStrip(
                  savedBytes: (savedBytes as num?)?.toDouble() ??
                      double.tryParse('$savedBytes') ??
                      0,
                  completed: completed,
                  efficiency:
                      (analytics['efficiency_percent'] as num?)?.toDouble() ??
                          0,
                ),
                _AutopilotSummary(
                  autopilot: autopilot,
                  onTap: () => controller.selectTab(3),
                ),
                if (controller.showSecondaryUi) ...[
                  const SectionHeader(title: 'Quick actions'),
                  Wrap(
                    spacing: 9,
                    runSpacing: 9,
                    children: [
                      FilledButton.tonalIcon(
                        onPressed: controller.canControl
                            ? () => _run(
                                  context,
                                  () => controller.setQueuePaused(!paused),
                                )
                            : null,
                        icon: Icon(
                          paused
                              ? Icons.play_arrow_rounded
                              : Icons.pause_rounded,
                        ),
                        label: Text(paused ? 'Resume queue' : 'Pause queue'),
                      ),
                      FilledButton.tonalIcon(
                        onPressed: () => controller.selectTab(1),
                        icon: const Icon(Icons.video_library_outlined),
                        label: const Text('Library'),
                      ),
                    ],
                  ),
                ],
                if (controller.statsForNerds) ...[
                  const SectionHeader(title: 'Recent server activity'),
                  _EventList(events: events),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _run(
    BuildContext context,
    Future<void> Function() action,
  ) async {
    try {
      await action();
    } catch (error) {
      if (!context.mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('$error')));
    }
  }
}

class _ReadyRow extends StatelessWidget {
  const _ReadyRow();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: ByteSqueezeColors.mint.withValues(alpha: .06),
        border: Border.all(color: ByteSqueezeColors.subtleLine),
        borderRadius: BorderRadius.circular(12),
      ),
      child: const Row(
        children: [
          Icon(
            Icons.check_circle_rounded,
            color: ByteSqueezeColors.mint,
            size: 20,
          ),
          SizedBox(width: 10),
          Expanded(child: Text('All encoders available')),
        ],
      ),
    );
  }
}

class _ActiveJobCard extends StatelessWidget {
  const _ActiveJobCard({required this.job});

  final Map<String, dynamic> job;

  @override
  Widget build(BuildContext context) {
    final progress =
        ((job['progress'] as num?)?.toDouble() ?? 0).clamp(0, 100).toDouble();
    final node = '${job['node_name'] ?? 'Main controller'}';
    final encoder = '${job['encoder'] ?? job['preset'] ?? 'Smart'}';
    final fps = (job['fps'] as num?)?.toDouble();
    final inputBytes = job['src_size_bytes'] ?? job['input_size_bytes'];
    final outputBytes = job['estimated_output_bytes'] ??
        ((job['estimated_out_gb'] as num?)?.toDouble() ?? 0) *
            1024 *
            1024 *
            1024;
    final hasSize =
        inputBytes != null && outputBytes is num && outputBytes.toDouble() > 0;
    return SurfaceCard(
      padding: const EdgeInsets.all(14),
      borderColor: ByteSqueezeColors.cyan.withValues(alpha: .32),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              const Icon(
                Icons.graphic_eq_rounded,
                color: ByteSqueezeColors.cyan,
                size: 22,
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      fileName(job['src']),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontWeight: FontWeight.w800),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      '$encoder · $node',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: ByteSqueezeColors.muted,
                        fontSize: 11.5,
                      ),
                    ),
                  ],
                ),
              ),
              Text(
                '${progress.toStringAsFixed(0)}%',
                style: const TextStyle(
                  fontWeight: FontWeight.w900,
                  color: ByteSqueezeColors.cyan,
                ),
              ),
            ],
          ),
          const SizedBox(height: 11),
          ClipRRect(
            borderRadius: BorderRadius.circular(999),
            child: LinearProgressIndicator(value: progress / 100, minHeight: 6),
          ),
          const SizedBox(height: 7),
          Text(
            '${fps != null && fps > 0 ? '${fps.toStringAsFixed(0)} FPS · ' : ''}${formatDuration(job['eta_seconds'])} left${hasSize ? ' · ${formatBytes(inputBytes)} → est. ${formatBytes(outputBytes)}' : ''}',
            style: const TextStyle(
              color: ByteSqueezeColors.muted,
              fontSize: 11.5,
            ),
          ),
        ],
      ),
    );
  }
}

class _StorageImpactCard extends StatelessWidget {
  const _StorageImpactCard({required this.summary, required this.analytics});

  final Map<String, dynamic> summary;
  final Map<String, dynamic> analytics;

  @override
  Widget build(BuildContext context) {
    final trend = asList(analytics['trend']).map(asMap).toList();
    final workers = asList(analytics['workers']).map(asMap).toList();
    final recent = asMap(analytics['recent']);
    final saved = summary['saved_bytes'] ?? 0;
    final videoSaved = (summary['video_saved_bytes'] as num?)?.toDouble() ?? 0;
    final audioSaved = (summary['audio_saved_bytes'] as num?)?.toDouble() ?? 0;
    final completed = (summary['count'] as num?)?.toInt() ?? 0;
    final efficiency =
        (analytics['efficiency_percent'] as num?)?.toDouble() ?? 0;
    final average = analytics['average_saved_bytes'] ?? 0;
    return SurfaceCard(
      padding: const EdgeInsets.all(15),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      'ALL-TIME RECOVERED',
                      style: TextStyle(
                        color: ByteSqueezeColors.muted,
                        fontSize: 9.5,
                        fontWeight: FontWeight.w800,
                        letterSpacing: 1.1,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      formatBytes(saved),
                      style: Theme.of(context)
                          .textTheme
                          .headlineMedium
                          ?.copyWith(color: ByteSqueezeColors.mint),
                    ),
                  ],
                ),
              ),
              _RingStat(value: efficiency, label: 'efficient'),
            ],
          ),
          if (videoSaved != 0 || audioSaved != 0) ...[
            const SizedBox(height: 14),
            _SavingsBreakdownBar(
                videoBytes: videoSaved, audioBytes: audioSaved),
          ],
          const SizedBox(height: 14),
          Row(
            children: [
              _InlineMetric(label: 'Completed', value: '$completed'),
              _InlineMetric(
                label: 'Average saved',
                value: formatBytes(average),
              ),
              _InlineMetric(
                label: '30 days',
                value: formatBytes(recent['saved_bytes'] ?? 0),
              ),
            ],
          ),
          if (trend.isNotEmpty) ...[
            const SizedBox(height: 17),
            Row(
              children: [
                const Expanded(
                  child: Text(
                    'Savings over the last 30 days',
                    style: TextStyle(fontWeight: FontWeight.w700),
                  ),
                ),
                Text(
                  '${recent['completed'] ?? 0} encodes',
                  style: const TextStyle(
                    color: ByteSqueezeColors.muted,
                    fontSize: 11,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 9),
            Semantics(
              label: 'Storage savings trend for the last 30 days',
              image: true,
              child: SizedBox(
                height: 104,
                child: CustomPaint(
                  painter: _SavingsChartPainter(
                    trend
                        .map(
                          (row) =>
                              (row['saved_bytes'] as num?)?.toDouble() ?? 0,
                        )
                        .toList(),
                  ),
                ),
              ),
            ),
          ],
          if (workers.isNotEmpty) ...[
            const SizedBox(height: 16),
            const Text(
              'Saved by worker',
              style: TextStyle(fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 8),
            ...workers.take(4).map((worker) => _WorkerContribution(worker)),
          ],
        ],
      ),
    );
  }
}

class _SavingsBreakdownBar extends StatelessWidget {
  const _SavingsBreakdownBar(
      {required this.videoBytes, required this.audioBytes});
  final double videoBytes;
  final double audioBytes;

  @override
  Widget build(BuildContext context) {
    final video = videoBytes.clamp(0, double.infinity).toDouble();
    final audio = audioBytes.clamp(0, double.infinity).toDouble();
    final total = video + audio;
    final videoShare = total > 0 ? video / total : 0.0;
    return Column(
      children: [
        ClipRRect(
          borderRadius: BorderRadius.circular(99),
          child: SizedBox(
            height: 9,
            child: Row(
              children: [
                if (videoShare > 0)
                  Expanded(
                      flex: (videoShare * 1000).round(),
                      child: const ColoredBox(color: ByteSqueezeColors.cyan)),
                if (audio > 0)
                  Expanded(
                      flex: ((1 - videoShare) * 1000)
                          .round()
                          .clamp(1, 1000)
                          .toInt(),
                      child: const ColoredBox(color: ByteSqueezeColors.violet)),
              ],
            ),
          ),
        ),
        const SizedBox(height: 9),
        Row(
          children: [
            Expanded(
                child: Text('● Video ${formatBytes(video)}',
                    style: const TextStyle(
                        color: ByteSqueezeColors.cyan,
                        fontSize: 12,
                        fontWeight: FontWeight.w700))),
            Expanded(
                child: Text('● Audio ${formatBytes(audio)}',
                    textAlign: TextAlign.end,
                    style: const TextStyle(
                        color: ByteSqueezeColors.violet,
                        fontSize: 12,
                        fontWeight: FontWeight.w700))),
          ],
        ),
      ],
    );
  }
}

class _InlineMetric extends StatelessWidget {
  const _InlineMetric({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            value,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontWeight: FontWeight.w800),
          ),
          const SizedBox(height: 2),
          Text(
            label,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              color: ByteSqueezeColors.muted,
              fontSize: 10.5,
            ),
          ),
        ],
      ),
    );
  }
}

class _RingStat extends StatelessWidget {
  const _RingStat({required this.value, required this.label});

  final double value;
  final String label;

  @override
  Widget build(BuildContext context) {
    final progress = (value / 100).clamp(0, 1).toDouble();
    return SizedBox(
      width: 70,
      height: 70,
      child: Stack(
        alignment: Alignment.center,
        children: [
          SizedBox.expand(
            child: CircularProgressIndicator(
              value: progress,
              strokeWidth: 6,
              backgroundColor: ByteSqueezeColors.raised,
              color: ByteSqueezeColors.mint,
            ),
          ),
          Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                '${value.toStringAsFixed(0)}%',
                style: const TextStyle(
                  fontWeight: FontWeight.w900,
                  fontSize: 14,
                ),
              ),
              Text(
                label,
                style: const TextStyle(
                  color: ByteSqueezeColors.muted,
                  fontSize: 8.5,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _WorkerContribution extends StatelessWidget {
  const _WorkerContribution(this.worker);

  final Map<String, dynamic> worker;

  @override
  Widget build(BuildContext context) {
    final share = ((worker['share_percent'] as num?)?.toDouble() ?? 0).clamp(
      0,
      100,
    );
    return Padding(
      padding: const EdgeInsets.only(bottom: 9),
      child: Column(
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  '${worker['display_name'] ?? worker['node_name'] ?? 'Worker'}',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontSize: 12.5),
                ),
              ),
              Text(
                '${worker['completed'] ?? 0} encodes',
                style: const TextStyle(
                  color: ByteSqueezeColors.muted,
                  fontSize: 10.5,
                ),
              ),
              const SizedBox(width: 8),
              Text(
                formatBytes(worker['saved_bytes']),
                style: const TextStyle(
                  color: ByteSqueezeColors.mint,
                  fontWeight: FontWeight.w700,
                  fontSize: 11.5,
                ),
              ),
            ],
          ),
          const SizedBox(height: 5),
          ClipRRect(
            borderRadius: BorderRadius.circular(999),
            child: LinearProgressIndicator(value: share / 100, minHeight: 4),
          ),
        ],
      ),
    );
  }
}

class _SavingsChartPainter extends CustomPainter {
  const _SavingsChartPainter(this.values);

  final List<double> values;

  @override
  void paint(Canvas canvas, Size size) {
    if (values.isEmpty || size.isEmpty) return;
    final maxValue = math.max(1.0, values.reduce(math.max));
    const inset = 3.0;
    final width = size.width - inset * 2;
    final height = size.height - inset * 2;
    final line = Paint()
      ..color = ByteSqueezeColors.cyan
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.2
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    final fill = Paint()
      ..shader = const LinearGradient(
        begin: Alignment.topCenter,
        end: Alignment.bottomCenter,
        colors: [Color(0x6648D7E8), Color(0x0048D7E8)],
      ).createShader(Rect.fromLTWH(0, 0, size.width, size.height));
    final grid = Paint()
      ..color = ByteSqueezeColors.subtleLine
      ..strokeWidth = 1;
    for (var row = 1; row < 4; row++) {
      final y = inset + (height * row / 4);
      canvas.drawLine(Offset(inset, y), Offset(size.width - inset, y), grid);
    }
    final path = Path();
    for (var index = 0; index < values.length; index++) {
      final x = inset +
          (values.length == 1
              ? width / 2
              : width * index / (values.length - 1));
      final y = inset + height - (values[index] / maxValue * height);
      if (index == 0) {
        path.moveTo(x, y);
      } else {
        path.lineTo(x, y);
      }
    }
    final area = Path.from(path)
      ..lineTo(size.width - inset, size.height - inset)
      ..lineTo(inset, size.height - inset)
      ..close();
    canvas.drawPath(area, fill);
    canvas.drawPath(path, line);
  }

  @override
  bool shouldRepaint(covariant _SavingsChartPainter oldDelegate) =>
      oldDelegate.values != values;
}

class _AchievementStrip extends StatelessWidget {
  const _AchievementStrip({
    required this.savedBytes,
    required this.completed,
    required this.efficiency,
  });

  final double savedBytes;
  final int completed;
  final double efficiency;

  @override
  Widget build(BuildContext context) {
    const tebibyte = 1099511627776.0;
    final achievements =
        <({String label, String detail, IconData icon, bool unlocked})>[
      (
        label: 'First squeeze',
        detail: 'First encode complete',
        icon: Icons.bolt_rounded,
        unlocked: completed >= 1,
      ),
      (
        label: 'Century club',
        detail: '$completed / 100 encodes',
        icon: Icons.workspace_premium_rounded,
        unlocked: completed >= 100,
      ),
      (
        label: 'Terabyte saver',
        detail: '${(savedBytes / tebibyte).toStringAsFixed(1)} TB reclaimed',
        icon: Icons.savings_rounded,
        unlocked: savedBytes >= tebibyte,
      ),
      (
        label: 'Efficiency expert',
        detail: '${efficiency.toStringAsFixed(0)}% smaller output',
        icon: Icons.auto_graph_rounded,
        unlocked: efficiency >= 50,
      ),
    ];
    return SizedBox(
      height: 94,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        itemCount: achievements.length,
        separatorBuilder: (_, __) => const SizedBox(width: 8),
        itemBuilder: (context, index) {
          final item = achievements[index];
          final color =
              item.unlocked ? ByteSqueezeColors.amber : ByteSqueezeColors.muted;
          return Container(
            width: 164,
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: item.unlocked
                  ? ByteSqueezeColors.amber.withValues(alpha: .07)
                  : ByteSqueezeColors.surface,
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: ByteSqueezeColors.subtleLine),
            ),
            child: Row(
              children: [
                Icon(item.icon, color: color, size: 24),
                const SizedBox(width: 9),
                Expanded(
                  child: Column(
                    mainAxisAlignment: MainAxisAlignment.center,
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        item.label,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          fontWeight: FontWeight.w800,
                          fontSize: 12,
                        ),
                      ),
                      const SizedBox(height: 3),
                      Text(
                        item.detail,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: ByteSqueezeColors.muted,
                          fontSize: 9.5,
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          );
        },
      ),
    );
  }
}

class _AutopilotSummary extends StatelessWidget {
  const _AutopilotSummary({required this.autopilot, required this.onTap});

  final Map<String, dynamic> autopilot;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final enabled = autopilot['enabled'] == true;
    final eligible = (autopilot['eligible'] as num?)?.toInt() ?? 0;
    return Padding(
      padding: const EdgeInsets.only(top: 16),
      child: Material(
        color: ByteSqueezeColors.surface,
        borderRadius: BorderRadius.circular(12),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(12),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
            child: Row(
              children: [
                Icon(
                  Icons.auto_awesome_rounded,
                  color: enabled
                      ? ByteSqueezeColors.cyan
                      : ByteSqueezeColors.muted,
                ),
                const SizedBox(width: 11),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text(
                        'Autopilot',
                        style: TextStyle(fontWeight: FontWeight.w800),
                      ),
                      Text(
                        enabled
                            ? '${autopilot['mode'] ?? 'observe'} · $eligible eligible titles'
                            : 'Disabled',
                        style: const TextStyle(
                          color: ByteSqueezeColors.muted,
                          fontSize: 11.5,
                        ),
                      ),
                    ],
                  ),
                ),
                const Icon(
                  Icons.chevron_right_rounded,
                  color: ByteSqueezeColors.muted,
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _EventList extends StatelessWidget {
  const _EventList({required this.events});

  final List<dynamic> events;

  @override
  Widget build(BuildContext context) {
    if (events.isEmpty) {
      return const Text(
        'No recent server events.',
        style: TextStyle(color: ByteSqueezeColors.muted),
      );
    }
    return Column(
      children: events.take(5).map((row) {
        final event = asMap(row);
        return ListTile(
          contentPadding: EdgeInsets.zero,
          leading: const Icon(
            Icons.bolt_rounded,
            size: 19,
            color: ByteSqueezeColors.cyan,
          ),
          title: Text(
            '${event['message'] ?? event['type'] ?? 'Server event'}',
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
          ),
          subtitle: Text(
            relativeTime(event['ts']),
            style: const TextStyle(color: ByteSqueezeColors.muted),
          ),
        );
      }).toList(),
    );
  }
}
