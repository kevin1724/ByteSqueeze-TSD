import 'package:flutter/material.dart';

import '../app_controller.dart';
import '../theme.dart';
import '../widgets/audio_optimizer_sheet.dart';
import '../widgets/common.dart';

class JobsScreen extends StatefulWidget {
  const JobsScreen({super.key, required this.controller});

  final AppController controller;

  @override
  State<JobsScreen> createState() => _JobsScreenState();
}

class _JobsScreenState extends State<JobsScreen> {
  bool _history = false;
  String _filter = 'all';

  @override
  Widget build(BuildContext context) {
    final allJobs = asList(widget.controller.jobs['jobs']).map(asMap).toList();
    final runningJobs = allJobs
        .where((job) => '${job['status'] ?? ''}'.toLowerCase() == 'running')
        .toList();
    const nonTerminalStates = {'running', 'queued', 'waiting_to_upload'};
    const queuedStates = {'queued', 'waiting_to_upload'};
    final rows = allJobs.where((job) {
      final status = '${job['status'] ?? ''}'.toLowerCase();
      final inGroup = _history
          ? !nonTerminalStates.contains(status)
          : queuedStates.contains(status);
      return inGroup && (_filter == 'all' || status == _filter);
    }).toList();
    final paused = widget.controller.jobs['paused'] == true;
    final summary = asMap(widget.controller.jobs['summary']);

    return RefreshIndicator(
      onRefresh: widget.controller.refreshAll,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        children: [
          PageInsets(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                CompactPageHeader(
                  title: 'Queue',
                  status: paused ? 'Paused' : '${runningJobs.length} running',
                  statusColor: paused
                      ? ByteSqueezeColors.amber
                      : (runningJobs.isEmpty
                          ? ByteSqueezeColors.mint
                          : ByteSqueezeColors.cyan),
                  summary:
                      "${summaryCount(summary, 'queued')} queued · ${summaryCount(summary, 'done')} completed · ${summaryCount(summary, 'error')} errors",
                  trailing: IconButton(
                    tooltip: paused ? 'Resume queue' : 'Pause queue',
                    onPressed: widget.controller.canControl
                        ? () => _run(
                              () => widget.controller.setQueuePaused(!paused),
                            )
                        : null,
                    icon: Icon(
                      paused ? Icons.play_arrow_rounded : Icons.pause_rounded,
                    ),
                  ),
                ),
                SectionHeader(
                  title: 'Running now',
                  subtitle: widget.controller.statsForNerds
                      ? _capacityLabel(summary, runningJobs.length)
                      : (runningJobs.isEmpty
                          ? 'All encoders are available'
                          : '${runningJobs.length} active encode${runningJobs.length == 1 ? '' : 's'}'),
                ),
                if (runningJobs.isEmpty)
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 14,
                      vertical: 12,
                    ),
                    decoration: BoxDecoration(
                      color: ByteSqueezeColors.mint.withValues(alpha: .06),
                      borderRadius: BorderRadius.circular(12),
                      border: Border.all(color: ByteSqueezeColors.subtleLine),
                    ),
                    child: const Row(
                      children: [
                        Icon(
                          Icons.check_circle_outline_rounded,
                          color: ByteSqueezeColors.mint,
                        ),
                        SizedBox(width: 12),
                        Expanded(
                          child: Text(
                            'All encoders available. The next queued item starts automatically.',
                          ),
                        ),
                      ],
                    ),
                  )
                else
                  ...runningJobs.map(
                    (job) => Padding(
                      padding: const EdgeInsets.only(bottom: 11),
                      child: _JobCard(
                        controller: widget.controller,
                        job: job,
                        run: _run,
                        editPreset: _editPreset,
                      ),
                    ),
                  ),
                const SizedBox(height: 18),
                SegmentedButton<bool>(
                  segments: const [
                    ButtonSegment(
                      value: false,
                      label: Text('Up next'),
                      icon: Icon(Icons.queue_play_next_rounded),
                    ),
                    ButtonSegment(
                      value: true,
                      label: Text('History'),
                      icon: Icon(Icons.history_rounded),
                    ),
                  ],
                  selected: {_history},
                  onSelectionChanged: (value) => setState(() {
                    _history = value.first;
                    _filter = 'all';
                  }),
                  showSelectedIcon: false,
                  style: ButtonStyle(
                    shape: WidgetStatePropertyAll(
                      RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(14),
                      ),
                    ),
                  ),
                ),
                if (widget.controller.showSecondaryUi) ...[
                  const SizedBox(height: 12),
                  SingleChildScrollView(
                    scrollDirection: Axis.horizontal,
                    child: Row(
                      children: (_history
                              ? const ['all', 'done', 'error', 'canceled']
                              : const [
                                  'all',
                                  'queued',
                                  'waiting_to_upload',
                                ])
                          .map(
                            (value) => Padding(
                              padding: const EdgeInsets.only(right: 8),
                              child: ChoiceChip(
                                selected: _filter == value,
                                onSelected: (_) =>
                                    setState(() => _filter = value),
                                label: Text(_filterLabel(value)),
                              ),
                            ),
                          )
                          .toList(),
                    ),
                  ),
                ],
                if (_history && rows.isNotEmpty)
                  Align(
                    alignment: Alignment.centerRight,
                    child: TextButton.icon(
                      onPressed: widget.controller.canControl
                          ? () => _confirmClear('finished')
                          : null,
                      icon: const Icon(Icons.delete_sweep_outlined),
                      label: const Text('Clear finished history'),
                    ),
                  )
                else if (!_history && rows.isNotEmpty)
                  Align(
                    alignment: Alignment.centerRight,
                    child: TextButton.icon(
                      onPressed: widget.controller.canControl
                          ? () => _confirmClear('queued')
                          : null,
                      icon: const Icon(Icons.playlist_remove_rounded),
                      label: const Text('Clear waiting jobs'),
                    ),
                  )
                else
                  const SizedBox(height: 8),
                if (paused && !_history)
                  const Padding(
                    padding: EdgeInsets.only(bottom: 12),
                    child: SurfaceCard(
                      borderColor: ByteSqueezeColors.amber,
                      child: Row(
                        children: [
                          Icon(
                            Icons.pause_circle_outline_rounded,
                            color: ByteSqueezeColors.amber,
                          ),
                          SizedBox(width: 12),
                          Expanded(
                            child: Text(
                              'The queue is paused. Running work may finish, but new jobs will not start.',
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                if (rows.isEmpty)
                  EmptyState(
                    icon: _history
                        ? Icons.history_toggle_off_rounded
                        : Icons.check_circle_outline_rounded,
                    title: _history ? 'No job history here' : 'Queue is clear',
                    message: _history
                        ? 'Completed, failed, and canceled jobs appear here.'
                        : 'Queue media from Library or let Autopilot choose eligible work.',
                  )
                else
                  ...rows.map(
                    (job) => Padding(
                      padding: const EdgeInsets.only(bottom: 11),
                      child: _JobCard(
                        controller: widget.controller,
                        job: job,
                        run: _run,
                        editPreset: _editPreset,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  String _capacityLabel(Map<String, dynamic> summary, int running) {
    final limit =
        (summary['hardware_transcode_concurrency'] as num?)?.toInt() ?? 1;
    if (running == 0) {
      return 'Controller GPU limit $limit · linked workers report here too';
    }
    return '$running active across controller and linked workers · controller GPU limit $limit';
  }

  String _filterLabel(String value) {
    if (value == 'all') return 'All';
    if (value == 'waiting_to_upload') return 'Waiting upload';
    return '${value[0].toUpperCase()}${value.substring(1)}';
  }

  Future<void> _confirmClear(String target) async {
    final queued = target == 'queued';
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(queued ? 'Clear waiting jobs?' : 'Clear finished jobs?'),
        content: Text(
          queued
              ? 'This removes queued jobs from the controller and linked workers. Running encodes and media files are not deleted.'
              : 'This removes finished job rows and logs from the controller and linked workers. Media files are not deleted.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Clear'),
          ),
        ],
      ),
    );
    if (confirmed == true) {
      await _run(() => widget.controller.clearJobs(target));
    }
  }

  Future<void> _editPreset(Map<String, dynamic> job) async {
    final selected = await showDialog<String>(
      context: context,
      builder: (context) => SimpleDialog(
        title: const Text('Edit queued preset'),
        children: [
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, 'smart'),
            child: const ListTile(
              leading: Icon(Icons.auto_awesome_rounded),
              title: Text('Smart Preset'),
              subtitle: Text('Use learned preferences and node hardware'),
            ),
          ),
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, 'auto'),
            child: const ListTile(
              leading: Icon(Icons.route_rounded),
              title: Text('Automatic'),
              subtitle: Text('Choose from the source and destination node'),
            ),
          ),
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, '1080'),
            child: const ListTile(
              leading: Icon(Icons.hd_rounded),
              title: Text('1080p'),
              subtitle: Text('Keep or limit output to 1920×1080'),
            ),
          ),
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, '4k'),
            child: const ListTile(
              leading: Icon(Icons.four_k_rounded),
              title: Text('4K'),
              subtitle: Text('Use the configured 4K preset'),
            ),
          ),
        ],
      ),
    );
    if (selected != null) {
      await _run(
        () => widget.controller.editJobPreset('${job['id'] ?? ''}', selected),
      );
    }
  }

  Future<void> _run(Future<void> Function() action) async {
    try {
      await action();
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('$error')));
    }
  }
}

class _JobCard extends StatelessWidget {
  const _JobCard({
    required this.controller,
    required this.job,
    required this.run,
    required this.editPreset,
  });

  final AppController controller;
  final Map<String, dynamic> job;
  final Future<void> Function(Future<void> Function()) run;
  final Future<void> Function(Map<String, dynamic>) editPreset;

  @override
  Widget build(BuildContext context) {
    final status = '${job['status'] ?? 'unknown'}'.toLowerCase();
    final color = statusColor(status);
    final progress =
        ((job['progress'] as num?)?.toDouble() ?? 0).clamp(0, 100).toDouble();
    final canMove = status == 'queued' && controller.canControl;
    final canEditPreset = status == 'queued' && controller.canControl;
    final canCancel =
        {'running', 'queued', 'waiting_to_upload'}.contains(status) &&
            controller.canControl;
    final terminal = {'done', 'error', 'canceled'}.contains(status);
    if (terminal) {
      final savings = asMap(job['storage_breakdown']);
      return Material(
        color: ByteSqueezeColors.surface,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 6, 8),
          child: Row(
            children: [
              Icon(
                status == 'done'
                    ? Icons.check_circle_rounded
                    : (status == 'error'
                        ? Icons.error_rounded
                        : Icons.cancel_rounded),
                color: color,
                size: 20,
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
                      style: const TextStyle(
                        fontWeight: FontWeight.w700,
                        fontSize: 13,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      status == 'done' && job['saved_bytes'] != null
                          ? '${formatBytes(job['saved_bytes'])} saved · ${job['node_name'] ?? 'Main controller'}'
                          : '${job['node_name'] ?? 'Main controller'} · ${status.replaceAll('_', ' ')}',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: ByteSqueezeColors.muted,
                        fontSize: 10.5,
                      ),
                    ),
                    if (status == 'done' && savings.isNotEmpty)
                      Text(
                        'Video ${formatBytes(savings['video_saved_bytes'])} · Audio ${formatBytes(savings['audio_saved_bytes'])}',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                            color: ByteSqueezeColors.muted, fontSize: 10.5),
                      ),
                  ],
                ),
              ),
              StatusPill(label: status.replaceAll('_', ' '), color: color),
              if (status == 'done' &&
                  controller.canControl &&
                  job['is_worker_job'] != true)
                IconButton(
                  tooltip: 'Optimize audio without re-encoding video',
                  icon: const Icon(Icons.graphic_eq_rounded,
                      color: ByteSqueezeColors.cyan),
                  onPressed: () async {
                    final queued = await showAudioOptimizerSheet(context,
                        controller: controller, job: job);
                    if (queued == true && context.mounted) {
                      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
                          content: Text(
                              'Audio-only job queued. Video will be copied.')));
                    }
                  },
                ),
              const SizedBox(width: 6),
            ],
          ),
        ),
      );
    }
    return SurfaceCard(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              DecoratedBox(
                decoration: BoxDecoration(
                  color: color.withValues(alpha: .12),
                  borderRadius: BorderRadius.circular(13),
                ),
                child: Padding(
                  padding: const EdgeInsets.all(11),
                  child: Icon(
                    status == 'done'
                        ? Icons.check_rounded
                        : Icons.movie_filter_rounded,
                    color: color,
                  ),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      fileName(job['src']),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontWeight: FontWeight.w700),
                    ),
                    const SizedBox(height: 5),
                    Text(
                      '${job['queued_preset_name'] ?? job['encoder'] ?? job['encode_method'] ?? job['preset'] ?? 'Automatic'} · ${job['node_name'] ?? (job['is_worker_job'] == true ? 'Linked worker' : 'Main controller')}',
                      style: const TextStyle(
                        color: ByteSqueezeColors.muted,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),
              PopupMenuButton<String>(
                enabled: controller.canControl,
                onSelected: (action) async {
                  if (action == 'edit_preset') {
                    await editPreset(job);
                  } else {
                    await run(
                      () => controller.jobAction('${job['id']}', action),
                    );
                  }
                },
                itemBuilder: (context) => [
                  if (canEditPreset)
                    const PopupMenuItem(
                      value: 'edit_preset',
                      child: ListTile(
                        leading: Icon(Icons.auto_awesome_rounded),
                        title: Text('Edit preset'),
                      ),
                    ),
                  if (canMove) ...const [
                    PopupMenuItem(
                      value: 'top',
                      child: ListTile(
                        leading: Icon(Icons.vertical_align_top_rounded),
                        title: Text('Move to top'),
                      ),
                    ),
                    PopupMenuItem(
                      value: 'up',
                      child: ListTile(
                        leading: Icon(Icons.keyboard_arrow_up_rounded),
                        title: Text('Move up'),
                      ),
                    ),
                    PopupMenuItem(
                      value: 'down',
                      child: ListTile(
                        leading: Icon(Icons.keyboard_arrow_down_rounded),
                        title: Text('Move down'),
                      ),
                    ),
                    PopupMenuItem(
                      value: 'bottom',
                      child: ListTile(
                        leading: Icon(Icons.vertical_align_bottom_rounded),
                        title: Text('Move to bottom'),
                      ),
                    ),
                  ],
                  if (canCancel)
                    const PopupMenuItem(
                      value: 'cancel',
                      child: ListTile(
                        leading: Icon(
                          Icons.cancel_outlined,
                          color: ByteSqueezeColors.danger,
                        ),
                        title: Text('Cancel job'),
                      ),
                    ),
                ],
              ),
            ],
          ),
          const SizedBox(height: 13),
          Row(
            children: [
              StatusPill(label: status.replaceAll('_', ' '), color: color),
              const Spacer(),
              if (status == 'running')
                Text(
                  '${progress.toStringAsFixed(0)}% · ${formatDuration(job['eta_seconds'])} left',
                  style: const TextStyle(
                    color: ByteSqueezeColors.muted,
                    fontSize: 12,
                  ),
                ),
              if (status == 'done' && job['saved_bytes'] != null)
                Text(
                  '${formatBytes(job['saved_bytes'])} saved',
                  style: const TextStyle(
                    color: ByteSqueezeColors.mint,
                    fontSize: 12,
                  ),
                ),
            ],
          ),
          if (status == 'running') ...[
            const SizedBox(height: 11),
            ClipRRect(
              borderRadius: BorderRadius.circular(999),
              child: LinearProgressIndicator(
                value: progress / 100,
                minHeight: 8,
              ),
            ),
          ],
          if ('${job['cancel_reason'] ?? ''}'.isNotEmpty) ...[
            const SizedBox(height: 10),
            Text(
              '${job['cancel_reason']}',
              style: const TextStyle(
                color: ByteSqueezeColors.danger,
                fontSize: 12,
              ),
            ),
          ],
        ],
      ),
    );
  }
}
