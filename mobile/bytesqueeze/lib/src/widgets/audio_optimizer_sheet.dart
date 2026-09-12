import 'package:flutter/material.dart';

import '../app_controller.dart';
import '../theme.dart';
import 'common.dart';

Future<bool?> showAudioOptimizerSheet(
  BuildContext context, {
  required AppController controller,
  required Map<String, dynamic> job,
}) {
  return showModalBottomSheet<bool>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    backgroundColor: ByteSqueezeColors.surface,
    builder: (_) => FractionallySizedBox(
      heightFactor: .94,
      child: AudioOptimizerSheet(controller: controller, job: job),
    ),
  );
}

class AudioOptimizerSheet extends StatefulWidget {
  const AudioOptimizerSheet({
    super.key,
    required this.controller,
    required this.job,
  });

  final AppController controller;
  final Map<String, dynamic> job;

  @override
  State<AudioOptimizerSheet> createState() => _AudioOptimizerSheetState();
}

class _AudioOptimizerSheetState extends State<AudioOptimizerSheet> {
  Map<String, dynamic> _inventory = {};
  Map<String, dynamic> _operations = {};
  bool _loading = true;
  bool _queueing = false;
  String _error = '';

  List<Map<String, dynamic>> get _actions =>
      asList(_operations['audio_actions'])
          .map((value) => Map<String, dynamic>.from(asMap(value)))
          .toList();

  @override
  void initState() {
    super.initState();
    _scan();
  }

  Future<void> _scan({Map<String, dynamic>? operations}) async {
    setState(() {
      _loading = true;
      _error = '';
    });
    try {
      final value = await widget.controller.scanAudio(
        path: '${widget.job['out_path'] ?? ''}',
        jobId: '${widget.job['id'] ?? ''}',
        operations: operations,
      );
      if (!mounted) return;
      setState(() {
        _inventory = Map<String, dynamic>.from(asMap(value['inventory']));
        _operations = Map<String, dynamic>.from(asMap(value['operations']));
        _loading = false;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = '$error';
      });
    }
  }

  void _updateAction(int index, Map<String, dynamic> next) {
    final actions = _actions;
    actions[index] = next;
    setState(() {
      _operations = {..._operations, 'audio_actions': actions};
      _recalculate(actions);
    });
  }

  void _recalculate(List<Map<String, dynamic>> actions) {
    final duration = (_inventory['duration_seconds'] as num?)?.toDouble() ?? 0;
    var current = 0.0;
    var output = 0.0;
    for (final action in actions) {
      final source = (action['source_size_bytes'] as num?)?.toDouble() ?? 0;
      current += source;
      if (action['action'] == 'copy') {
        output += source;
      } else if (action['action'] == 'encode') {
        output += action['target_codec'] == 'flac'
            ? source * .72
            : ((action['bitrate_kbps'] as num?)?.toDouble() ?? 1024) *
                1000 *
                duration /
                8;
      }
    }
    _operations['estimate'] = {
      'current_audio_bytes': current.round(),
      'output_audio_bytes': output.round(),
      'audio_savings_bytes': (current - output).round(),
      'audio_savings_percent':
          current > 0 ? (current - output) / current * 100 : 0,
      'estimated': true,
    };
  }

  Future<void> _queue() async {
    setState(() {
      _queueing = true;
      _error = '';
    });
    try {
      final validated = await widget.controller.scanAudio(
        path: '${widget.job['out_path'] ?? ''}',
        jobId: '${widget.job['id'] ?? ''}',
        operations: _operations,
      );
      final jobId = '${widget.job['id'] ?? ''}';
      if (jobId.isNotEmpty) {
        await widget.controller.queueAudioOptimization(
          jobId,
          asMap(validated['operations']),
        );
      } else {
        await widget.controller.queueAudioOptimizationPath(
          '${widget.job['out_path'] ?? widget.job['src'] ?? ''}',
          asMap(validated['operations']),
        );
      }
      if (mounted) Navigator.pop(context, true);
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _queueing = false;
        _error = '$error';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final estimate = asMap(_operations['estimate']);
    final warnings = asList(_operations['warnings']);
    final video = asList(_inventory['video_streams']).map(asMap).firstOrNull;
    final audioSizesExact = _inventory['audio_sizes_exact'] == true;
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 12, 10, 10),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('Optimize audio',
                        style: TextStyle(
                            fontSize: 22, fontWeight: FontWeight.w800)),
                    Text(fileName(widget.job['out_path'] ?? widget.job['src']),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(color: ByteSqueezeColors.muted)),
                  ],
                ),
              ),
              IconButton(
                  onPressed: () => Navigator.pop(context),
                  icon: const Icon(Icons.close_rounded)),
            ],
          ),
        ),
        const Divider(height: 1),
        Expanded(
          child: _loading
              ? const Center(child: CircularProgressIndicator())
              : _error.isNotEmpty && _operations.isEmpty
                  ? _ErrorState(message: _error, retry: _scan)
                  : ListView(
                      padding: const EdgeInsets.fromLTRB(16, 16, 16, 120),
                      children: [
                        Container(
                          padding: const EdgeInsets.all(14),
                          decoration: BoxDecoration(
                              color:
                                  ByteSqueezeColors.mint.withValues(alpha: .07),
                              borderRadius: BorderRadius.circular(12),
                              border: Border.all(
                                  color: ByteSqueezeColors.subtleLine)),
                          child: Row(children: [
                            const Icon(Icons.verified_rounded,
                                color: ByteSqueezeColors.mint),
                            const SizedBox(width: 12),
                            Expanded(
                                child: Text(
                                    'Video: ${(video?['codec'] ?? 'video').toString().toUpperCase()} · COPY\nNo video re-encode. Subtitles, chapters and attachments are preserved.',
                                    style: const TextStyle(fontSize: 13))),
                          ]),
                        ),
                        const SizedBox(height: 14),
                        Row(children: [
                          Expanded(
                              child: _Estimate(
                                  label: audioSizesExact
                                      ? 'Current audio'
                                      : 'Current audio (estimated)',
                                  value: formatBytes(
                                      estimate['current_audio_bytes']))),
                          const Icon(Icons.arrow_forward_rounded,
                              color: ByteSqueezeColors.muted),
                          Expanded(
                              child: _Estimate(
                                  label: 'Estimated output',
                                  value: formatBytes(
                                      estimate['output_audio_bytes']))),
                        ]),
                        const SizedBox(height: 8),
                        Text(
                            'Potential savings ${formatBytes(estimate['audio_savings_bytes'])} · ${((estimate['audio_savings_percent'] as num?)?.toDouble() ?? 0).toStringAsFixed(0)}%',
                            style: const TextStyle(
                                color: ByteSqueezeColors.cyan,
                                fontWeight: FontWeight.w700)),
                        if (warnings.isNotEmpty) ...[
                          const SizedBox(height: 12),
                          ...warnings.map((warning) => Padding(
                              padding: const EdgeInsets.only(bottom: 6),
                              child: Text('⚠ $warning',
                                  style: const TextStyle(
                                      color: ByteSqueezeColors.amber,
                                      fontSize: 12)))),
                        ],
                        const SizedBox(height: 18),
                        const Text('Audio tracks',
                            style: TextStyle(
                                fontSize: 17, fontWeight: FontWeight.w800)),
                        const SizedBox(height: 8),
                        ..._actions.asMap().entries.map((entry) =>
                            _AudioTrackEditor(
                                action: entry.value,
                                onChanged: (next) =>
                                    _updateAction(entry.key, next))),
                        if (_error.isNotEmpty)
                          Padding(
                              padding: const EdgeInsets.only(top: 12),
                              child: Text(_error,
                                  style: const TextStyle(
                                      color: ByteSqueezeColors.danger))),
                      ],
                    ),
        ),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 10, 16, 12),
            child: FilledButton.icon(
              onPressed: _loading ||
                      _queueing ||
                      !_operations.containsKey('audio_actions')
                  ? null
                  : _queue,
              icon: _queueing
                  ? const SizedBox.square(
                      dimension: 18,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.queue_play_next_rounded),
              label: Text(_queueing
                  ? 'Validating…'
                  : 'Next available node · Queue audio optimization'),
            ),
          ),
        ),
      ],
    );
  }
}

class _Estimate extends StatelessWidget {
  const _Estimate({required this.label, required this.value});
  final String label;
  final String value;
  @override
  Widget build(BuildContext context) => Padding(
      padding: const EdgeInsets.all(10),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(label,
            style:
                const TextStyle(color: ByteSqueezeColors.muted, fontSize: 11)),
        const SizedBox(height: 3),
        Text(value,
            style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w800))
      ]));
}

class _AudioTrackEditor extends StatelessWidget {
  const _AudioTrackEditor({required this.action, required this.onChanged});
  final Map<String, dynamic> action;
  final ValueChanged<Map<String, dynamic>> onChanged;

  @override
  Widget build(BuildContext context) {
    final encode = action['action'] == 'encode';
    final channels = (action['channels'] as num?)?.toInt() ?? 2;
    final sourceChannels =
        (action['source_channels'] as num?)?.toInt() ?? channels;
    final bitrate = (action['bitrate_kbps'] as num?)?.toInt() ?? 1024;
    final bitrateChoices = <int>{
      256,
      320,
      448,
      640,
      768,
      1024,
      1280,
      1536,
      2048,
      bitrate,
    }.toList()
      ..sort();
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: ExpansionTile(
        key: ValueKey(
          '${action['stream_index']}-${action['action']}-${action['target_codec']}-${action['bitrate_kbps']}',
        ),
        tilePadding: const EdgeInsets.symmetric(horizontal: 12),
        childrenPadding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
        collapsedShape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
            side: const BorderSide(color: ByteSqueezeColors.subtleLine)),
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
            side: const BorderSide(color: ByteSqueezeColors.line)),
        title: Text(
            '${('${action['language'] ?? 'und'}').toUpperCase()} · ${action['source_codec_label'] ?? action['source_codec'] ?? 'Audio'} · ${sourceChannels}ch',
            style: const TextStyle(fontWeight: FontWeight.w700)),
        subtitle: Text(
            '${formatBytes(action['source_size_bytes'])} · ${action['action'].toString().toUpperCase()}',
            style: const TextStyle(color: ByteSqueezeColors.muted)),
        trailing: DropdownButton<String>(
          value: '${action['action'] ?? 'copy'}',
          underline: const SizedBox.shrink(),
          items: const [
            DropdownMenuItem(value: 'copy', child: Text('COPY')),
            DropdownMenuItem(value: 'encode', child: Text('ENCODE')),
            DropdownMenuItem(value: 'remove', child: Text('REMOVE'))
          ],
          onChanged: (value) {
            if (value != null) {
              onChanged({...action, 'action': value});
            }
          },
        ),
        children: [
          if (encode) ...[
            DropdownButtonFormField<String>(
              initialValue: '${action['target_codec'] ?? 'aac'}',
              decoration: const InputDecoration(labelText: 'Target codec'),
              items: const [
                DropdownMenuItem(value: 'aac', child: Text('AAC')),
                DropdownMenuItem(value: 'eac3', child: Text('E-AC3')),
                DropdownMenuItem(value: 'ac3', child: Text('AC3')),
                DropdownMenuItem(value: 'opus', child: Text('Opus')),
                DropdownMenuItem(value: 'flac', child: Text('FLAC'))
              ],
              onChanged: (value) {
                if (value != null) {
                  onChanged({...action, 'target_codec': value});
                }
              },
            ),
            const SizedBox(height: 9),
            Row(children: [
              Expanded(
                  child: DropdownButtonFormField<int>(
                      initialValue: bitrate,
                      decoration: const InputDecoration(labelText: 'Bitrate'),
                      items: bitrateChoices
                          .map((value) => DropdownMenuItem(
                                value: value,
                                child: Text(
                                  '$value kbps${value == 1024 ? ' · safe' : ''}',
                                ),
                              ))
                          .toList(),
                      onChanged: (value) {
                        if (value != null) {
                          onChanged({...action, 'bitrate_kbps': value});
                        }
                      })),
              const SizedBox(width: 9),
              Expanded(
                  child: DropdownButtonFormField<int>(
                      initialValue: channels.clamp(1, 8),
                      decoration: const InputDecoration(labelText: 'Channels'),
                      items: List.generate(
                          8,
                          (index) => DropdownMenuItem(
                              value: index + 1, child: Text('${index + 1}'))),
                      onChanged: (value) {
                        if (value != null) {
                          onChanged({
                            ...action,
                            'channels': value,
                            if (value >= sourceChannels) 'allow_downmix': false,
                          });
                        }
                      })),
            ]),
            if (channels < sourceChannels)
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Allow downmix'),
                subtitle: Text(
                    '$sourceChannels → $channels channels is destructive and requires explicit approval.'),
                value: action['allow_downmix'] == true,
                onChanged: (value) {
                  onChanged({...action, 'allow_downmix': value});
                },
              ),
            Row(children: [
              Expanded(
                  child: TextFormField(
                      initialValue: '${action['sample_rate'] ?? ''}',
                      keyboardType: TextInputType.number,
                      decoration:
                          const InputDecoration(labelText: 'Sample rate'),
                      onChanged: (value) {
                        onChanged({
                          ...action,
                          'sample_rate': int.tryParse(value) ?? 0,
                        });
                      })),
              const SizedBox(width: 9),
              Expanded(
                  child: TextFormField(
                      initialValue: '${action['gain_db'] ?? 0}',
                      keyboardType: const TextInputType.numberWithOptions(
                          decimal: true, signed: true),
                      decoration: const InputDecoration(labelText: 'Gain dB'),
                      onChanged: (value) {
                        onChanged({
                          ...action,
                          'gain_db': double.tryParse(value) ?? 0,
                        });
                      })),
            ]),
          ],
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Preserve track metadata'),
            value: action['preserve_metadata'] != false,
            onChanged: action['action'] == 'remove'
                ? null
                : (value) {
                    onChanged({...action, 'preserve_metadata': value});
                  },
          ),
          if (action['object_audio'] == true || action['commentary'] == true)
            const Text(
                'This track may carry Atmos/DTS:X or commentary metadata. Copy is the safest option.',
                style: TextStyle(color: ByteSqueezeColors.amber, fontSize: 12)),
        ],
      ),
    );
  }
}

class _ErrorState extends StatelessWidget {
  const _ErrorState({required this.message, required this.retry});
  final String message;
  final VoidCallback retry;
  @override
  Widget build(BuildContext context) => Center(
      child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Icon(Icons.error_outline_rounded,
                color: ByteSqueezeColors.danger, size: 42),
            const SizedBox(height: 12),
            Text(message, textAlign: TextAlign.center),
            const SizedBox(height: 16),
            OutlinedButton(onPressed: retry, child: const Text('Try again'))
          ])));
}
