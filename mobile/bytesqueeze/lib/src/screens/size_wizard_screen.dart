import 'dart:async';

import 'package:flutter/material.dart';

import '../app_controller.dart';
import '../theme.dart';
import '../widgets/common.dart';

@visibleForTesting
Map<String, dynamic> applySizeWizardOption(
  Map<String, dynamic> current,
  String key,
  dynamic value,
) {
  final updated = Map<String, dynamic>.from(current)..[key] = value;
  if (key == 'audio_mode') {
    // Smart Presets safely begin with passthrough locked. Selecting another
    // mode in this screen is an explicit per-job override, so clear the stale
    // lock before the estimate request is sent back to the server.
    if (value != 'copy') updated['smart_never_transcode_audio'] = false;
    updated['ai_copy_audio'] = value == 'copy';
    updated['smart_audio_strategy'] =
        value == 'copy' ? 'copy' : (value == 'eac3' ? 'eac3_surround' : '');
    if (value == 'eac3' && '${updated['audio_bitrate'] ?? 'auto'}' == 'auto') {
      updated['audio_bitrate'] = '1024';
    }
  }
  return updated;
}

class SizeWizardScreen extends StatefulWidget {
  const SizeWizardScreen({
    super.key,
    required this.controller,
    required this.path,
    required this.title,
  });

  final AppController controller;
  final String path;
  final String title;

  @override
  State<SizeWizardScreen> createState() => _SizeWizardScreenState();
}

class _SizeWizardScreenState extends State<SizeWizardScreen> {
  final _targetSize = TextEditingController();
  Map<String, dynamic> _response = <String, dynamic>{};
  Map<String, dynamic> _plan = <String, dynamic>{};
  Map<String, dynamic> _options = <String, dynamic>{};
  bool _loading = true;
  bool _queueing = false;
  bool _dirty = false;
  String _error = '';
  String _destination = 'local';
  String _candidateId = 'manual';
  Timer? _estimateDebounce;
  int _editRevision = 0;

  AppController get controller => widget.controller;

  @override
  void initState() {
    super.initState();
    _load(smartStart: true);
  }

  @override
  void dispose() {
    _estimateDebounce?.cancel();
    _targetSize.dispose();
    super.dispose();
  }

  Future<void> _load({required bool smartStart, int? revision}) async {
    if (smartStart) _editRevision += 1;
    final requestedRevision = revision ?? _editRevision;
    setState(() {
      _loading = true;
      _error = '';
    });
    try {
      final value = await controller.planSizeWizard(
        widget.path,
        smartStart: smartStart,
        options: smartStart ? null : _queueOptions(),
        smartCandidateId: _candidateId,
      );
      if (!mounted || requestedRevision != _editRevision) return;
      _applyResponse(value);
    } catch (error) {
      if (!mounted || requestedRevision != _editRevision) return;
      setState(() => _error = '$error');
    } finally {
      if (mounted && requestedRevision == _editRevision) {
        setState(() => _loading = false);
      }
    }
  }

  void _applyResponse(Map<String, dynamic> value) {
    final plan = asMap(value['plan']);
    final options = Map<String, dynamic>.from(asMap(plan['options']));
    setState(() {
      _response = value;
      _plan = plan;
      _options = options;
      _candidateId = '${value['smart_candidate_id'] ?? 'manual'}';
      _targetSize.text = '${options['target_size_value'] ?? 1}';
      _dirty = false;
    });
  }

  Map<String, dynamic> _queueOptions() {
    final value = Map<String, dynamic>.from(_options);
    final parsed = double.tryParse(_targetSize.text.trim());
    if (parsed != null && parsed > 0) value['target_size_value'] = parsed;
    // A mobile Wizard plan is explicit after the user touches it. Keep the
    // source frame rate locked even when another option is customized.
    value['ai_mode'] = false;
    value['framerate_mode'] = 'same';
    value['smart_lock_source_framerate'] = true;
    return value;
  }

  void _setOption(String key, dynamic value) {
    setState(() {
      _options = applySizeWizardOption(_options, key, value);
      _dirty = true;
      _editRevision += 1;
    });
    _scheduleEstimate();
  }

  void _targetChanged() {
    setState(() {
      _dirty = true;
      _editRevision += 1;
    });
    _scheduleEstimate();
  }

  void _scheduleEstimate() {
    _estimateDebounce?.cancel();
    final revision = _editRevision;
    _estimateDebounce = Timer(
      const Duration(milliseconds: 550),
      () => _load(smartStart: false, revision: revision),
    );
  }

  Future<void> _queue() async {
    if (_queueing || _loading) return;
    setState(() {
      _queueing = true;
      _error = '';
    });
    try {
      final value = await controller.queueSizeWizard(
        widget.path,
        _queueOptions(),
        smartCandidateId: _candidateId,
        mode: _destination,
      );
      if (!mounted) return;
      final learned = value['learning_recorded'] == true;
      Navigator.pop(context, true);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            learned
                ? 'Queued and saved as a Smart Preset preference.'
                : 'Queued. This source was already active, so learning was not duplicated.',
          ),
        ),
      );
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = '$error');
    } finally {
      if (mounted) setState(() => _queueing = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final probe = asMap(_plan['probe']);
    final estimates = asMap(_plan['estimates']);
    final output = asMap(estimates['output_resolution']);
    final autoTarget = asMap(estimates['auto_target']);
    final learned = asMap(_response['learned_defaults']);
    final sourceWidth = (probe['width'] as num?)?.toInt() ?? 0;
    final sourceHeight = (probe['height'] as num?)?.toInt() ?? 0;
    final outputWidth = (output['width'] as num?)?.toInt() ?? sourceWidth;
    final outputHeight = (output['height'] as num?)?.toInt() ?? sourceHeight;
    final sourceBytes = (probe['source_size_bytes'] as num?)?.toDouble() ?? 0;
    final estimatedMb =
        (estimates['estimated_output_mb'] as num?)?.toDouble() ?? 0;
    final outputBytes = estimatedMb * 1024 * 1024;
    final savedBytes =
        (sourceBytes - outputBytes).clamp(0, double.infinity).toDouble();
    final savingsPercent =
        sourceBytes > 0 ? savedBytes / sourceBytes * 100 : 0.0;
    final encoderLabel =
        '${estimates['encoder_label'] ?? estimates['encoder'] ?? 'Smart encoder'}';

    return Scaffold(
      appBar: AppBar(title: const Text('Size Wizard')),
      body: _loading && _plan.isEmpty
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.fromLTRB(16, 10, 16, 120),
              children: [
                Text(
                  widget.title,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.headlineSmall,
                ),
                const SizedBox(height: 5),
                Text(
                  sourceWidth > 0
                      ? '$sourceWidth×$sourceHeight · ${probe['is_hdr'] == true ? 'HDR' : 'SDR'} · source FPS preserved'
                      : 'Source FPS is always preserved',
                  style: const TextStyle(color: ByteSqueezeColors.muted),
                ),
                const SizedBox(height: 12),
                _EstimateHero(
                  sourceBytes: sourceBytes,
                  outputBytes: outputBytes,
                  savedBytes: savedBytes,
                  savingsPercent: savingsPercent,
                  outputWidth: outputWidth,
                  outputHeight: outputHeight,
                  encoder: encoderLabel,
                  hdr: probe['is_hdr'] == true,
                  loading: _loading,
                ),
                const SizedBox(height: 10),
                Material(
                  color: ByteSqueezeColors.surface,
                  borderRadius: BorderRadius.circular(12),
                  child: ListTile(
                    dense: true,
                    leading: const Icon(
                      Icons.auto_awesome_rounded,
                      color: ByteSqueezeColors.cyan,
                    ),
                    title: Text(
                      '${_response['smart_candidate_name'] ?? 'Smart starting point'}',
                      style: const TextStyle(fontWeight: FontWeight.w700),
                    ),
                    subtitle: Text(
                      learned['sample_count'] == null
                          ? 'Uses saved Smart Preset guardrails'
                          : '${learned['sample_count']} similar choices considered',
                    ),
                    trailing: IconButton(
                      tooltip: 'Restore Smart starting point',
                      onPressed:
                          _loading ? null : () => _load(smartStart: true),
                      icon: const Icon(Icons.restore_rounded),
                    ),
                  ),
                ),
                const SectionHeader(title: 'Size and picture'),
                SurfaceCard(
                  child: Column(
                    children: [
                      SwitchListTile.adaptive(
                        contentPadding: EdgeInsets.zero,
                        value: _options['target_size_auto'] != false,
                        onChanged: (value) =>
                            _setOption('target_size_auto', value),
                        title: const Text('Automatic target size'),
                        subtitle: Text(
                          _options['target_size_auto'] == false
                              ? 'Use an exact file-size target.'
                              : '${autoTarget['summary'] ?? 'Calculate from this title and your learned preferences.'}',
                        ),
                      ),
                      if (_options['target_size_auto'] == false) ...[
                        const SizedBox(height: 8),
                        Row(
                          children: [
                            Expanded(
                              child: TextField(
                                controller: _targetSize,
                                keyboardType:
                                    const TextInputType.numberWithOptions(
                                  decimal: true,
                                ),
                                onChanged: (_) => _targetChanged(),
                                decoration: const InputDecoration(
                                  labelText: 'Target size',
                                ),
                              ),
                            ),
                            const SizedBox(width: 10),
                            SizedBox(
                              width: 105,
                              child: _dropdown(
                                label: 'Unit',
                                value:
                                    '${_options['target_size_unit'] ?? 'GB'}',
                                values: const ['MB', 'GB'],
                                onChanged: (value) =>
                                    _setOption('target_size_unit', value),
                              ),
                            ),
                          ],
                        ),
                      ],
                      const SizedBox(height: 12),
                      _dropdown(
                        label: 'Resolution',
                        value: '${_options['resolution_mode'] ?? 'keep'}',
                        values: const [
                          'auto',
                          'keep',
                          '2160',
                          '1440',
                          '1080',
                          '720',
                        ],
                        labels: const {
                          'auto': 'Smart choice',
                          'keep': 'Keep source',
                          '2160': 'Up to 4K',
                          '1440': 'Up to 1440p',
                          '1080': 'Up to 1080p',
                          '720': 'Up to 720p',
                        },
                        onChanged: (value) =>
                            _setOption('resolution_mode', value),
                      ),
                      const SizedBox(height: 12),
                      _dropdown(
                        label: 'Quality goal',
                        value: '${_options['quality'] ?? 'balanced'}',
                        values: const ['high', 'balanced', 'small'],
                        labels: const {
                          'high': 'High quality',
                          'balanced': 'Balanced',
                          'small': 'Smaller file',
                        },
                        onChanged: (value) => _setOption('quality', value),
                      ),
                    ],
                  ),
                ),
                const SectionHeader(title: 'Encode choices'),
                SurfaceCard(
                  padding: EdgeInsets.zero,
                  child: ExpansionTile(
                    initiallyExpanded: controller.showSecondaryUi,
                    title: const Text(
                      'Codec, hardware, audio, and subtitles',
                      style: TextStyle(fontWeight: FontWeight.w800),
                    ),
                    subtitle: Text(
                      '${_options['encoder_family'] ?? 'software'} · ${_options['video_codec'] ?? 'h265'} ${_options['bit_depth'] ?? '10'}-bit · ${_options['audio_mode'] ?? 'copy'} audio',
                      style: const TextStyle(
                        color: ByteSqueezeColors.muted,
                        fontSize: 12,
                      ),
                    ),
                    childrenPadding: const EdgeInsets.fromLTRB(14, 0, 14, 16),
                    children: [
                      _dropdown(
                        label: 'Video codec',
                        value: '${_options['video_codec'] ?? 'h265'}',
                        values: const ['h265', 'h264', 'av1'],
                        labels: const {
                          'h265': 'H.265 / HEVC',
                          'h264': 'H.264',
                          'av1': 'AV1',
                        },
                        onChanged: (value) => _setOption('video_codec', value),
                      ),
                      const SizedBox(height: 11),
                      _dropdown(
                        label: 'Encoder',
                        value: '${_options['encoder_family'] ?? 'software'}',
                        values: const ['software', 'qsv'],
                        labels: const {
                          'software': 'Software / CPU',
                          'qsv': 'Intel Quick Sync',
                        },
                        onChanged: (value) =>
                            _setOption('encoder_family', value),
                      ),
                      const SizedBox(height: 11),
                      Row(
                        children: [
                          Expanded(
                            child: _dropdown(
                              label: 'Bit depth',
                              value: '${_options['bit_depth'] ?? '10'}',
                              values: const ['8', '10'],
                              onChanged: (value) =>
                                  _setOption('bit_depth', value),
                            ),
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: _dropdown(
                              label: 'Speed',
                              value: '${_options['encoder_speed'] ?? 'auto'}',
                              values: const ['auto', 'fast', 'medium', 'slow'],
                              onChanged: (value) =>
                                  _setOption('encoder_speed', value),
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 11),
                      _dropdown(
                        label: 'Audio',
                        value: '${_options['audio_mode'] ?? 'copy'}',
                        values: const ['copy', 'eac3', 'aac', 'auto'],
                        labels: const {
                          'copy': 'Passthrough / copy',
                          'eac3': 'Smart lossless optimize · 1024k',
                          'aac': 'AAC · choose bitrate',
                          'auto': 'Automatic · quality first',
                        },
                        onChanged: (value) => _setOption('audio_mode', value),
                      ),
                      if ('${_options['audio_mode'] ?? 'copy'}' != 'copy') ...[
                        const SizedBox(height: 11),
                        _dropdown(
                          label: 'Audio bitrate',
                          value: '${_options['audio_bitrate'] ?? '1024'}',
                          values: const [
                            'auto',
                            '640',
                            '768',
                            '1024',
                            '1280',
                            '1536',
                            '2048',
                          ],
                          labels: const {
                            'auto': 'Automatic (quality first)',
                            '1024': '1024 kbps · recommended surround',
                          },
                          onChanged: (value) =>
                              _setOption('audio_bitrate', value),
                        ),
                      ],
                      const SizedBox(height: 11),
                      _dropdown(
                        label: 'Subtitles',
                        value: '${_options['subtitle_mode'] ?? 'all'}',
                        values: const ['all', 'first', 'none'],
                        labels: const {
                          'all': 'Keep all',
                          'first': 'First matching',
                          'none': 'None',
                        },
                        onChanged: (value) =>
                            _setOption('subtitle_mode', value),
                      ),
                    ],
                  ),
                ),
                if (_dirty) ...[
                  const SizedBox(height: 12),
                  OutlinedButton.icon(
                    onPressed: _loading
                        ? null
                        : () =>
                            _load(smartStart: false, revision: _editRevision),
                    icon: _loading
                        ? const SizedBox(
                            width: 17,
                            height: 17,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.calculate_outlined),
                    label: const Text('Update estimate'),
                  ),
                ],
                if (controller.statsForNerds) ...[
                  const SectionHeader(title: 'Stats for nerds'),
                  SurfaceCard(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        _resultRow(
                          'Source FPS',
                          '${probe['fps'] ?? 'unknown'}',
                        ),
                        _resultRow(
                          'Video bitrate',
                          '${estimates['video_bitrate_kbps'] ?? 'unknown'} kbps',
                        ),
                        _resultRow(
                          'Source bytes',
                          '${probe['source_size_bytes'] ?? 'unknown'}',
                        ),
                        const SizedBox(height: 8),
                        const Text(
                          'Source path',
                          style: TextStyle(
                            color: ByteSqueezeColors.muted,
                            fontSize: 11,
                          ),
                        ),
                        SelectableText(
                          widget.path,
                          style: const TextStyle(fontSize: 11),
                        ),
                      ],
                    ),
                  ),
                ],
                if (_error.isNotEmpty) ...[
                  const SizedBox(height: 12),
                  Text(
                    _error,
                    style: const TextStyle(color: ByteSqueezeColors.danger),
                  ),
                ],
                const SectionHeader(title: 'Queue destination'),
                _dropdown(
                  label: 'Run on',
                  value: _destination,
                  values: const ['local', 'available'],
                  labels: const {
                    'local': 'Main controller',
                    'available': 'Next available node',
                  },
                  onChanged: (value) => setState(() => _destination = value),
                ),
              ],
            ),
      bottomNavigationBar: SafeArea(
        minimum: const EdgeInsets.fromLTRB(14, 8, 14, 10),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.only(left: 4, right: 4, bottom: 7),
              child: Text(
                '${_destination == 'available' ? 'Next available node' : 'Main controller'} · $encoderLabel',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  color: ByteSqueezeColors.muted,
                  fontSize: 11.5,
                ),
              ),
            ),
            FilledButton.icon(
              onPressed: controller.canControl && !_loading && !_queueing
                  ? _queue
                  : null,
              icon: _queueing
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.add_to_queue_rounded),
              label: Text(_queueing ? 'Queueing…' : 'Queue encode'),
              style: FilledButton.styleFrom(
                padding: const EdgeInsets.symmetric(vertical: 14),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _dropdown({
    required String label,
    required String value,
    required List<String> values,
    Map<String, String> labels = const {},
    required ValueChanged<String> onChanged,
  }) {
    final safeValue = values.contains(value) ? value : values.first;
    return DropdownButtonFormField<String>(
      key: ValueKey('$label-$safeValue'),
      initialValue: safeValue,
      decoration: InputDecoration(labelText: label),
      items: values
          .map(
            (item) => DropdownMenuItem(
              value: item,
              child: Text(labels[item] ?? item),
            ),
          )
          .toList(),
      onChanged: _loading
          ? null
          : (next) {
              if (next != null) onChanged(next);
            },
    );
  }

  Widget _resultRow(String label, String value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 5),
        child: Row(
          children: [
            Expanded(
              child: Text(
                label,
                style: const TextStyle(color: ByteSqueezeColors.muted),
              ),
            ),
            const SizedBox(width: 12),
            Flexible(
              child: Text(
                value,
                textAlign: TextAlign.right,
                style: const TextStyle(fontWeight: FontWeight.w700),
              ),
            ),
          ],
        ),
      );
}

class _EstimateHero extends StatelessWidget {
  const _EstimateHero({
    required this.sourceBytes,
    required this.outputBytes,
    required this.savedBytes,
    required this.savingsPercent,
    required this.outputWidth,
    required this.outputHeight,
    required this.encoder,
    required this.hdr,
    required this.loading,
  });

  final double sourceBytes;
  final double outputBytes;
  final double savedBytes;
  final double savingsPercent;
  final int outputWidth;
  final int outputHeight;
  final String encoder;
  final bool hdr;
  final bool loading;

  @override
  Widget build(BuildContext context) {
    final ready = sourceBytes > 0 && outputBytes > 0;
    return SurfaceCard(
      padding: const EdgeInsets.all(16),
      borderColor: ByteSqueezeColors.cyan.withValues(alpha: .36),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (loading) ...[
            const LinearProgressIndicator(minHeight: 2),
            const SizedBox(height: 12),
          ],
          Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      'CURRENT SIZE',
                      style: TextStyle(
                        color: ByteSqueezeColors.muted,
                        fontSize: 9,
                        fontWeight: FontWeight.w800,
                        letterSpacing: 1,
                      ),
                    ),
                    const SizedBox(height: 3),
                    FittedBox(
                      fit: BoxFit.scaleDown,
                      alignment: Alignment.centerLeft,
                      child: Text(
                        ready ? formatBytes(sourceBytes) : '—',
                        style: Theme.of(context).textTheme.headlineSmall,
                      ),
                    ),
                  ],
                ),
              ),
              const Padding(
                padding: EdgeInsets.symmetric(horizontal: 12),
                child: Icon(
                  Icons.arrow_forward_rounded,
                  color: ByteSqueezeColors.muted,
                ),
              ),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    const Text(
                      'ESTIMATED SIZE',
                      style: TextStyle(
                        color: ByteSqueezeColors.cyan,
                        fontSize: 9,
                        fontWeight: FontWeight.w800,
                        letterSpacing: 1,
                      ),
                    ),
                    const SizedBox(height: 3),
                    FittedBox(
                      fit: BoxFit.scaleDown,
                      alignment: Alignment.centerRight,
                      child: Text(
                        ready ? formatBytes(outputBytes) : 'Calculating',
                        style: Theme.of(context)
                            .textTheme
                            .headlineSmall
                            ?.copyWith(color: ByteSqueezeColors.cyan),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 14),
          ClipRRect(
            borderRadius: BorderRadius.circular(999),
            child: LinearProgressIndicator(
              value: ready ? (savingsPercent / 100).clamp(0, 1).toDouble() : 0,
              minHeight: 7,
              color: ByteSqueezeColors.mint,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            ready
                ? 'Save ~${formatBytes(savedBytes)} · ${savingsPercent.toStringAsFixed(0)}% smaller'
                : 'Building a title-specific estimate…',
            style: const TextStyle(
              color: ByteSqueezeColors.mint,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: 5),
          Text(
            '$encoder · ${outputWidth > 0 ? '$outputWidth×$outputHeight' : 'source resolution'} · ${hdr ? 'HDR preserved' : 'SDR'} · source FPS',
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
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
