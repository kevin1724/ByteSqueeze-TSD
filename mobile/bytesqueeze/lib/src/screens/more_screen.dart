import 'package:flutter/material.dart';

import '../app_controller.dart';
import '../app_meta.dart';
import '../theme.dart';
import '../widgets/common.dart';
import 'calendar_screen.dart';

class MoreScreen extends StatelessWidget {
  const MoreScreen({super.key, required this.controller});

  final AppController controller;

  @override
  Widget build(BuildContext context) {
    final nodeRows = asList(controller.nodes['nodes']);
    final online =
        nodeRows.where((row) => asMap(row)['online'] == true).length +
            (asMap(controller.nodes['local'])['online'] == false ? 0 : 1);
    final storageSummary = asMap(controller.storage['summary']);
    final eventRows = asList(controller.events['events']);
    return ListView(
      children: [
        PageInsets(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              CompactPageHeader(
                title: 'More',
                status: controller.demoMode ? 'Demo' : 'Connected',
                statusColor: controller.demoMode
                    ? ByteSqueezeColors.violet
                    : ByteSqueezeColors.mint,
                summary: controller.statsForNerds
                    ? controller.serverLabel
                    : '${controller.canControl ? 'Control' : 'Read-only'} access · $online nodes online',
              ),
              const SectionHeader(title: 'Encoding'),
              _SettingsGroup(
                children: [
                  _MoreTile(
                    icon: Icons.developer_board_rounded,
                    color: ByteSqueezeColors.cyan,
                    title: 'Encoding capacity & safety',
                    subtitle: !controller.statsForNerds
                        ? 'Encoder capacity and safety limits'
                        : controller.serverSupportsOperationsSettings
                            ? '${controller.operations['hardware_transcode_concurrency'] ?? asMap(controller.jobs['summary'])['hardware_transcode_concurrency'] ?? 1} GPU slots · CPU stays at one'
                            : 'Server update needed for mobile controls',
                    onTap: () => _open(
                      context,
                      OperationsSettingsPage(controller: controller),
                    ),
                  ),
                ],
              ),
              const SectionHeader(title: 'Library'),
              _SettingsGroup(
                children: [
                  _MoreTile(
                    icon: Icons.calendar_month_rounded,
                    color: ByteSqueezeColors.blue,
                    title: 'Upcoming episodes',
                    subtitle: 'Release calendar for tracked shows',
                    onTap: () =>
                        _open(context, CalendarScreen(controller: controller)),
                  ),
                ],
              ),
              const SectionHeader(title: 'Nodes & storage'),
              _SettingsGroup(
                children: [
                  _MoreTile(
                    icon: Icons.hub_rounded,
                    color: ByteSqueezeColors.cyan,
                    title: 'Encoding nodes',
                    subtitle: controller.statsForNerds
                        ? '$online online · ${nodeRows.length + 1} total'
                        : 'Manage the main controller and linked workers',
                    onTap: () =>
                        _open(context, NodesPage(controller: controller)),
                  ),
                  _MoreTile(
                    icon: Icons.savings_outlined,
                    color: ByteSqueezeColors.mint,
                    title: 'Storage savings',
                    subtitle: controller.statsForNerds
                        ? '${formatBytes(storageSummary['saved_bytes'])} reclaimed across ${storageSummary['count'] ?? 0} encodes'
                        : 'Review the space ByteSqueeze reclaimed',
                    onTap: () =>
                        _open(context, StoragePage(controller: controller)),
                  ),
                ],
              ),
              const SectionHeader(title: 'App'),
              _SettingsGroup(
                children: [
                  _MoreTile(
                    icon: Icons.dashboard_customize_rounded,
                    color: ByteSqueezeColors.violet,
                    title: 'Interface',
                    subtitle: controller.useV3
                        ? '${controller.compactInterface ? 'Compact' : 'Comfortable'} · optional UI controls'
                        : 'V2 Classic is active',
                    onTap: () =>
                        _open(context, InterfacePage(controller: controller)),
                  ),
                  _MoreTile(
                    icon: Icons.security_rounded,
                    color: ByteSqueezeColors.blue,
                    title: 'Connection & security',
                    subtitle: 'Server address, access scope, and secure tokens',
                    onTap: () =>
                        _open(context, ConnectionPage(controller: controller)),
                  ),
                ],
              ),
              if (controller.statsForNerds) ...[
                const SectionHeader(title: 'Diagnostics'),
                _SettingsGroup(
                  children: [
                    if (controller.statsForNerds)
                      _MoreTile(
                        icon: Icons.bolt_rounded,
                        color: ByteSqueezeColors.amber,
                        title: 'Event timeline',
                        subtitle: '${eventRows.length} recent server events',
                        onTap: () =>
                            _open(context, EventsPage(controller: controller)),
                      ),
                  ],
                ),
              ],
              const SectionHeader(title: 'About'),
              _SettingsGroup(
                children: [
                  _MoreTile(
                    icon: Icons.info_outline_rounded,
                    color: ByteSqueezeColors.muted,
                    title: 'About',
                    subtitle: 'ByteSqueeze $appVersion',
                    onTap: () => showAboutDialog(
                      context: context,
                      applicationName: 'ByteSqueeze',
                      applicationVersion: '$appVersion+$appBuildNumber',
                      applicationIcon: ClipRRect(
                        borderRadius: BorderRadius.circular(18),
                        child: Image.asset(
                          'assets/branding/bytesqueeze_icon.png',
                          width: 72,
                          height: 72,
                        ),
                      ),
                      children: const [
                        Text(
                          'A cross-platform remote control for HandBrake TSD Helper. All encoding stays on the Docker-hosted server.',
                        ),
                      ],
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 18),
              OutlinedButton.icon(
                onPressed: () => _disconnect(context),
                icon: const Icon(
                  Icons.logout_rounded,
                  color: ByteSqueezeColors.danger,
                ),
                label: Text(
                  controller.demoMode ? 'Exit demo' : 'Disconnect this device',
                  style: const TextStyle(color: ByteSqueezeColors.danger),
                ),
                style: OutlinedButton.styleFrom(
                  padding: const EdgeInsets.symmetric(vertical: 15),
                  side: BorderSide(
                    color: ByteSqueezeColors.danger.withValues(alpha: .45),
                  ),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(16),
                  ),
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }

  void _open(BuildContext context, Widget page) {
    Navigator.push(context, MaterialPageRoute(builder: (_) => page));
  }

  Future<void> _disconnect(BuildContext context) async {
    final confirmed = controller.demoMode ||
        await showDialog<bool>(
              context: context,
              builder: (context) => AlertDialog(
                title: const Text('Disconnect ByteSqueeze?'),
                content: const Text(
                  'The server address and mobile tokens will be removed from this device. You can pair it again later.',
                ),
                actions: [
                  TextButton(
                    onPressed: () => Navigator.pop(context, false),
                    child: const Text('Cancel'),
                  ),
                  FilledButton(
                    onPressed: () => Navigator.pop(context, true),
                    child: const Text('Disconnect'),
                  ),
                ],
              ),
            ) ==
            true;
    if (confirmed) await controller.disconnect();
  }
}

class InterfacePage extends StatefulWidget {
  const InterfacePage({super.key, required this.controller});

  final AppController controller;

  @override
  State<InterfacePage> createState() => _InterfacePageState();
}

class _InterfacePageState extends State<InterfacePage> {
  AppController get controller => widget.controller;

  @override
  Widget build(BuildContext context) {
    return _DetailScaffold(
      title: 'Interface & UI',
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          SurfaceCard(
            gradient: const LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [Color(0xFF183D64), Color(0xFF11162B)],
            ),
            borderColor: ByteSqueezeColors.cyan.withValues(alpha: .35),
            child: const Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(
                  Icons.auto_awesome_rounded,
                  color: ByteSqueezeColors.cyan,
                  size: 30,
                ),
                SizedBox(width: 13),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'V3 is the current ByteSqueeze experience',
                        style: TextStyle(
                          fontWeight: FontWeight.w800,
                          fontSize: 17,
                        ),
                      ),
                      SizedBox(height: 5),
                      Text(
                        'Your server, pairing, library, queue, and Smart Preset data stay unchanged when the interface switches.',
                        style: TextStyle(color: ByteSqueezeColors.softInk),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          const SectionHeader(
            title: 'App experience',
            subtitle: 'Switch instantly; no restart or re-pairing required',
          ),
          _ExperienceOption(
            selected: controller.useV3,
            icon: Icons.view_quilt_rounded,
            color: ByteSqueezeColors.cyan,
            title: 'V3',
            badge: 'RECOMMENDED',
            description:
                'Adaptive workspace, command center, cleaner navigation, live operations dock, and focused settings.',
            onTap: () => _setVersion('v3'),
          ),
          const SizedBox(height: 10),
          _ExperienceOption(
            selected: !controller.useV3,
            icon: Icons.view_sidebar_outlined,
            color: ByteSqueezeColors.blue,
            title: 'V2 Classic',
            badge: 'FALLBACK',
            description:
                'The previous navigation shell remains available as an optional classic interface.',
            onTap: () => _setVersion('v2'),
          ),
          const SectionHeader(
            title: 'Information density',
            subtitle: 'Choose how much space controls and cards use',
          ),
          SegmentedButton<String>(
            segments: const [
              ButtonSegment(
                value: 'comfortable',
                label: Text('Comfortable'),
                icon: Icon(Icons.space_bar_rounded),
              ),
              ButtonSegment(
                value: 'compact',
                label: Text('Compact'),
                icon: Icon(Icons.density_small_rounded),
              ),
            ],
            selected: {controller.interfaceDensity},
            showSelectedIcon: false,
            onSelectionChanged:
                controller.useV3 ? (values) => _setDensity(values.first) : null,
          ),
          const SizedBox(height: 12),
          Text(
            controller.useV3
                ? 'This preference is stored on this phone only.'
                : 'Density is saved and will apply when you return to V3.',
            style: const TextStyle(
              color: ByteSqueezeColors.muted,
              fontSize: 12,
            ),
          ),
          const SectionHeader(
            title: 'UI visibility',
            subtitle:
                'Keep everyday screens focused; restore optional detail here',
          ),
          SurfaceCard(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            child: Column(
              children: [
                SwitchListTile.adaptive(
                  value: controller.showSecondaryUi,
                  onChanged: _setSecondaryUi,
                  secondary: const Icon(
                    Icons.widgets_outlined,
                    color: ByteSqueezeColors.cyan,
                  ),
                  title: const Text('Show secondary controls'),
                  subtitle: const Text(
                    'Restores helper cards, extra filters, refresh buttons, and the live operations dock.',
                  ),
                ),
                const Divider(height: 1),
                SwitchListTile.adaptive(
                  value: controller.statsForNerds,
                  onChanged: _setStatsForNerds,
                  secondary: const Icon(
                    Icons.data_object_rounded,
                    color: ByteSqueezeColors.violet,
                  ),
                  title: const Text('Stats for nerds'),
                  subtitle: const Text(
                    'Shows exact paths, server addresses, bitrates, event logs, and technical counters.',
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _setVersion(String version) async {
    await controller.setInterfaceVersion(version);
    if (mounted) setState(() {});
  }

  Future<void> _setDensity(String density) async {
    await controller.setInterfaceDensity(density);
    if (mounted) setState(() {});
  }

  Future<void> _setSecondaryUi(bool value) async {
    await controller.setShowSecondaryUi(value);
    if (mounted) setState(() {});
  }

  Future<void> _setStatsForNerds(bool value) async {
    await controller.setStatsForNerds(value);
    if (mounted) setState(() {});
  }
}

class _SettingsGroup extends StatelessWidget {
  const _SettingsGroup({required this.children});

  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: ByteSqueezeColors.surface,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: const BorderSide(color: ByteSqueezeColors.subtleLine),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        children: [
          for (var index = 0; index < children.length; index++) ...[
            children[index],
            if (index != children.length - 1)
              const Divider(indent: 52, endIndent: 12),
          ],
        ],
      ),
    );
  }
}

class _ExperienceOption extends StatelessWidget {
  const _ExperienceOption({
    required this.selected,
    required this.icon,
    required this.color,
    required this.title,
    required this.badge,
    required this.description,
    required this.onTap,
  });

  final bool selected;
  final IconData icon;
  final Color color;
  final String title;
  final String badge;
  final String description;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return SurfaceCard(
      onTap: onTap,
      borderColor: selected ? color : ByteSqueezeColors.line,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: color.withValues(alpha: .12),
              borderRadius: BorderRadius.circular(14),
            ),
            child: Icon(icon, color: color),
          ),
          const SizedBox(width: 13),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        title,
                        style: Theme.of(context).textTheme.titleLarge,
                      ),
                    ),
                    StatusPill(label: badge, color: color),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  description,
                  style: const TextStyle(color: ByteSqueezeColors.muted),
                ),
              ],
            ),
          ),
          const SizedBox(width: 8),
          Icon(
            selected ? Icons.check_circle_rounded : Icons.circle_outlined,
            color: selected ? color : ByteSqueezeColors.muted,
          ),
        ],
      ),
    );
  }
}

class OperationsSettingsPage extends StatefulWidget {
  const OperationsSettingsPage({super.key, required this.controller});

  final AppController controller;

  @override
  State<OperationsSettingsPage> createState() => _OperationsSettingsPageState();
}

class _OperationsSettingsPageState extends State<OperationsSettingsPage> {
  late int _hardwareSlots;
  late bool _autoStop;
  late double _stopPercent;
  late String _audioPolicy;
  late String _audioCodec;
  late int _audioBitrate;
  late bool _deduplicateAudio;
  late TextEditingController _audioLanguagesController;
  late bool _allowObjectAudioLoss;
  bool _saving = false;

  AppController get controller => widget.controller;

  @override
  void initState() {
    super.initState();
    final operations = controller.operations;
    final summary = asMap(controller.jobs['summary']);
    _hardwareSlots = ((operations['hardware_transcode_concurrency'] ??
            summary['hardware_transcode_concurrency'] ??
            1) as num)
        .toInt()
        .clamp(1, 8)
        .toInt();
    _autoStop = operations['auto_stop_large_output_enabled'] == true;
    _stopPercent =
        ((operations['auto_stop_large_output_percent'] as num?)?.toDouble() ??
                90)
            .clamp(50, 150)
            .toDouble();
    _audioPolicy = '${operations['audio_policy_default'] ?? 'preserve'}';
    if (!{'preserve', 'optimize_lossless', 'custom'}.contains(_audioPolicy)) {
      _audioPolicy = 'preserve';
    }
    _audioCodec = '${operations['audio_optimize_codec'] ?? 'aac'}';
    if (!{'aac', 'eac3', 'ac3', 'opus', 'flac'}.contains(_audioCodec)) {
      _audioCodec = 'aac';
    }
    _audioBitrate =
        ((operations['audio_optimize_bitrate_kbps'] as num?)?.toInt() ?? 1024)
            .clamp(640, 2048);
    _deduplicateAudio = operations['audio_deduplicate_languages'] != false;
    final languages = asList(operations['audio_preferred_languages'])
        .map((value) => '$value'.trim())
        .where((value) => value.isNotEmpty)
        .toList();
    _audioLanguagesController = TextEditingController(
      text: (languages.isEmpty ? const ['eng', 'spa'] : languages).join(', '),
    );
    _allowObjectAudioLoss =
        operations['audio_allow_object_metadata_loss'] == true;
  }

  @override
  void dispose() {
    _audioLanguagesController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final qsv = controller.operations['qsv_device_available'] == true;
    final supported = controller.serverSupportsOperationsSettings;
    return _DetailScaffold(
      title: 'Encoding capacity & safety',
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (!supported) ...[
            SurfaceCard(
              borderColor: ByteSqueezeColors.amber.withValues(alpha: .48),
              child: const Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(
                    Icons.system_update_alt_rounded,
                    color: ByteSqueezeColors.amber,
                  ),
                  SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Server update required',
                          style: TextStyle(fontWeight: FontWeight.w800),
                        ),
                        SizedBox(height: 4),
                        Text(
                          'The rest of ByteSqueeze remains connected. Update the TSD server to version 3.15 or newer to change these encoder controls from the app.',
                          style: TextStyle(
                            color: ByteSqueezeColors.softInk,
                            fontSize: 12.5,
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 12),
          ],
          SurfaceCard(
            gradient: const LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [Color(0xFF0E4B5C), Color(0xFF091822)],
            ),
            borderColor: ByteSqueezeColors.cyan.withValues(alpha: .38),
            child: Row(
              children: [
                const Icon(
                  Icons.developer_board_rounded,
                  color: ByteSqueezeColors.cyan,
                  size: 34,
                ),
                const SizedBox(width: 13),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        '$_hardwareSlots simultaneous GPU job${_hardwareSlots == 1 ? '' : 's'}',
                        style: Theme.of(context).textTheme.titleLarge,
                      ),
                      const SizedBox(height: 3),
                      const Text(
                        'CPU/software encoding always stays at one',
                        style: TextStyle(color: ByteSqueezeColors.softInk),
                      ),
                    ],
                  ),
                ),
                StatusPill(
                  label: !supported
                      ? 'LEGACY SERVER'
                      : (qsv ? 'QSV READY' : 'GPU STATUS UNKNOWN'),
                  color: supported && qsv
                      ? ByteSqueezeColors.mint
                      : ByteSqueezeColors.amber,
                ),
              ],
            ),
          ),
          const SectionHeader(
            title: 'Hardware concurrency',
            subtitle:
                'Raise this only for Intel QSV or another supported hardware encoder',
          ),
          SurfaceCard(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    const Expanded(
                      child: Text(
                        'Simultaneous hardware transcodes',
                        style: TextStyle(fontWeight: FontWeight.w700),
                      ),
                    ),
                    Container(
                      width: 48,
                      height: 42,
                      alignment: Alignment.center,
                      decoration: BoxDecoration(
                        color: ByteSqueezeColors.cyan.withValues(alpha: .12),
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Text(
                        '$_hardwareSlots',
                        style: const TextStyle(
                          color: ByteSqueezeColors.cyan,
                          fontSize: 20,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                    ),
                  ],
                ),
                Slider(
                  value: _hardwareSlots.toDouble(),
                  min: 1,
                  max: 8,
                  divisions: 7,
                  label: '$_hardwareSlots',
                  onChanged: controller.canControl && supported
                      ? (value) =>
                          setState(() => _hardwareSlots = value.round())
                      : null,
                ),
                const Text(
                  'ByteSqueeze fills available GPU slots. A software encode remains exclusive and will never run beside another transcode.',
                  style: TextStyle(
                    color: ByteSqueezeColors.muted,
                    fontSize: 12,
                  ),
                ),
              ],
            ),
          ),
          const SectionHeader(
            title: 'Output-size protection',
            subtitle: 'Stop an encode whose projected output is not useful',
          ),
          SurfaceCard(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                SwitchListTile.adaptive(
                  value: _autoStop,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Stop unexpectedly large outputs'),
                  subtitle: const Text(
                    'Uses encode checkpoints; the original source remains safe.',
                    style: TextStyle(color: ByteSqueezeColors.muted),
                  ),
                  onChanged: controller.canControl && supported
                      ? (value) => setState(() => _autoStop = value)
                      : null,
                ),
                if (_autoStop) ...[
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      const Expanded(
                        child: Text('Stop at projected source size'),
                      ),
                      Text(
                        '${_stopPercent.round()}%',
                        style: const TextStyle(
                          color: ByteSqueezeColors.amber,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                    ],
                  ),
                  Slider(
                    value: _stopPercent,
                    min: 50,
                    max: 150,
                    divisions: 20,
                    label: '${_stopPercent.round()}%',
                    onChanged: controller.canControl && supported
                        ? (value) => setState(() => _stopPercent = value)
                        : null,
                  ),
                ],
              ],
            ),
          ),
          const SectionHeader(
            title: 'Audio optimization',
            subtitle: 'Safe defaults for normal video jobs',
          ),
          SurfaceCard(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                DropdownButtonFormField<String>(
                  initialValue: _audioPolicy,
                  decoration:
                      const InputDecoration(labelText: 'Default audio policy'),
                  items: const [
                    DropdownMenuItem(
                        value: 'preserve',
                        child: Text('Preserve / Passthrough')),
                    DropdownMenuItem(
                        value: 'optimize_lossless',
                        child: Text('Optimize lossless audio')),
                    DropdownMenuItem(
                        value: 'custom', child: Text('Custom per job')),
                  ],
                  onChanged: controller.canControl && supported
                      ? (value) =>
                          setState(() => _audioPolicy = value ?? 'preserve')
                      : null,
                ),
                const SizedBox(height: 10),
                DropdownButtonFormField<String>(
                  initialValue: _audioCodec,
                  decoration:
                      const InputDecoration(labelText: 'Optimization codec'),
                  items: const [
                    DropdownMenuItem(value: 'aac', child: Text('AAC')),
                    DropdownMenuItem(value: 'eac3', child: Text('E-AC3')),
                    DropdownMenuItem(value: 'ac3', child: Text('AC3')),
                    DropdownMenuItem(value: 'opus', child: Text('Opus')),
                    DropdownMenuItem(value: 'flac', child: Text('FLAC')),
                  ],
                  onChanged: controller.canControl && supported
                      ? (value) => setState(() => _audioCodec = value ?? 'aac')
                      : null,
                ),
                const SizedBox(height: 10),
                DropdownButtonFormField<int>(
                  initialValue: _audioBitrate,
                  decoration: const InputDecoration(
                    labelText: 'Surround quality target',
                    helperText: '1024 kbps is recommended for 5.1 and 7.1.',
                  ),
                  items: const [640, 768, 1024, 1280, 1536, 2048]
                      .map((value) => DropdownMenuItem(
                            value: value,
                            child: Text(
                              '$value kbps${value == 1024 ? ' · recommended' : ''}',
                            ),
                          ))
                      .toList(),
                  onChanged: controller.canControl && supported
                      ? (value) => setState(() => _audioBitrate = value ?? 1024)
                      : null,
                ),
                const SizedBox(height: 10),
                TextFormField(
                  controller: _audioLanguagesController,
                  decoration: const InputDecoration(
                    labelText: 'Preferred languages',
                    hintText: 'eng, spa',
                    helperText: 'Comma-separated ISO codes.',
                  ),
                  enabled: controller.canControl && supported,
                ),
                SwitchListTile.adaptive(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('One best track per language'),
                  subtitle: const Text(
                    'Keeps the strongest English track and separate strongest Latino and Castilian Spanish tracks when both exist.',
                    style: TextStyle(
                      color: ByteSqueezeColors.muted,
                      fontSize: 12,
                    ),
                  ),
                  value: _deduplicateAudio,
                  onChanged: controller.canControl && supported
                      ? (value) => setState(() => _deduplicateAudio = value)
                      : null,
                ),
                SwitchListTile.adaptive(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Allow object-audio conversion'),
                  subtitle: const Text(
                      'Off is safest. Encoding TrueHD/DTS-HD may remove Atmos or DTS:X metadata.',
                      style: TextStyle(
                          color: ByteSqueezeColors.muted, fontSize: 12)),
                  value: _allowObjectAudioLoss,
                  onChanged: controller.canControl && supported
                      ? (value) => setState(() => _allowObjectAudioLoss = value)
                      : null,
                ),
                const Text(
                    'Preserve copies everything. Smart optimization passes through efficient 5.1 AC3/E-AC3 tracks and shows every proposed removal before queueing.',
                    style: TextStyle(
                        color: ByteSqueezeColors.muted, fontSize: 12)),
              ],
            ),
          ),
          const SizedBox(height: 14),
          FilledButton.icon(
            onPressed:
                controller.canControl && supported && !_saving ? _save : null,
            icon: _saving
                ? const SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.save_rounded),
            label: const Text('Save encoder settings'),
          ),
          const SizedBox(height: 10),
          Text(
            supported
                ? 'These are server settings and apply to the web interface, Autopilot, linked workers, and every paired ByteSqueeze device.'
                : 'No connection was lost. This page is read-only until the server is updated.',
            textAlign: TextAlign.center,
            style: const TextStyle(
              color: ByteSqueezeColors.muted,
              fontSize: 11.5,
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    try {
      await controller.saveOperationsSettings({
        'hardware_transcode_concurrency': _hardwareSlots,
        'auto_stop_large_output_enabled': _autoStop,
        'auto_stop_large_output_percent': _stopPercent.round(),
        'audio_policy_default': _audioPolicy,
        'audio_optimize_codec': _audioCodec,
        'audio_optimize_bitrate_kbps': _audioBitrate,
        'audio_deduplicate_languages': _deduplicateAudio,
        'audio_preferred_languages': _audioLanguagesController.text
            .split(RegExp(r'[,;]+'))
            .map((value) => value.trim().toLowerCase())
            .where((value) => value.isNotEmpty)
            .toList(),
        'audio_allow_object_metadata_loss': _allowObjectAudioLoss,
      });
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Encoder capacity and safety settings saved.'),
        ),
      );
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$error')));
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }
}

class _MoreTile extends StatelessWidget {
  const _MoreTile({
    required this.icon,
    required this.color,
    required this.title,
    required this.subtitle,
    required this.onTap,
  });

  final IconData icon;
  final Color color;
  final String title;
  final String subtitle;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return ListTile(
      onTap: onTap,
      minTileHeight: 58,
      contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 2),
      leading: Icon(icon, color: color, size: 22),
      title: Text(title, style: const TextStyle(fontWeight: FontWeight.w700)),
      subtitle: Text(
        subtitle,
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
        style: const TextStyle(color: ByteSqueezeColors.muted, fontSize: 12),
      ),
      trailing: const Icon(
        Icons.chevron_right_rounded,
        color: ByteSqueezeColors.muted,
      ),
    );
  }
}

class _DetailScaffold extends StatelessWidget {
  const _DetailScaffold({
    required this.title,
    required this.child,
    this.actions,
  });

  final String title;
  final Widget child;
  final List<Widget>? actions;

  @override
  Widget build(BuildContext context) {
    final classic =
        Theme.of(context).colorScheme.primary == ByteSqueezeColors.blue;
    return Scaffold(
      appBar: AppBar(title: Text(title), actions: actions),
      body: DecoratedBox(
        decoration: BoxDecoration(
          color: classic ? null : ByteSqueezeColors.canvas,
          gradient: classic ? ByteSqueezeColors.backdrop : null,
        ),
        child: child,
      ),
    );
  }
}

class NodesPage extends StatefulWidget {
  const NodesPage({super.key, required this.controller});

  final AppController controller;

  @override
  State<NodesPage> createState() => _NodesPageState();
}

class _NodesPageState extends State<NodesPage> {
  Future<void> _run(
    Future<void> Function() action, {
    String success = 'Linked node updated.',
  }) async {
    try {
      await action();
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(success)));
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('$error')));
    }
  }

  Future<void> _rename(Map<String, dynamic> node) async {
    final input = TextEditingController(text: '${node['name'] ?? 'Worker'}');
    final name = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Rename linked worker'),
        content: TextField(
          controller: input,
          autofocus: true,
          maxLength: 80,
          decoration: const InputDecoration(
            labelText: 'Worker name',
            helperText: 'This controller alias stays after reconnects.',
          ),
          onSubmitted: (value) => Navigator.pop(context, value.trim()),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, input.text.trim()),
            child: const Text('Save'),
          ),
        ],
      ),
    );
    input.dispose();
    if (name == null || name.isEmpty) return;
    await _run(
      () => widget.controller.nodeAction(
        '${node['id'] ?? ''}',
        'rename',
        name: name,
      ),
      success: 'Worker renamed to $name.',
    );
  }

  Future<void> _capacity(Map<String, dynamic> node) async {
    final current =
        ((node['hardware_transcode_concurrency'] as num?)?.toInt() ?? 1).clamp(
      1,
      8,
    );
    final selected = await showDialog<int>(
      context: context,
      builder: (context) => SimpleDialog(
        title: const Text('Simultaneous GPU jobs'),
        children: [
          for (var value = 1; value <= 8; value++)
            SimpleDialogOption(
              onPressed: () => Navigator.pop(context, value),
              child: ListTile(
                leading: Icon(
                  value == current
                      ? Icons.radio_button_checked_rounded
                      : Icons.radio_button_off_rounded,
                ),
                title: Text('$value GPU job${value == 1 ? '' : 's'}'),
                subtitle: value == 1
                    ? const Text('CPU/software work always remains exclusive')
                    : null,
              ),
            ),
        ],
      ),
    );
    if (selected == null) return;
    await _run(
      () => widget.controller.nodeAction(
        '${node['id'] ?? ''}',
        'capacity',
        hardwareTranscodeConcurrency: selected,
      ),
      success: 'Worker GPU capacity set to $selected.',
    );
  }

  Future<void> _confirmNodeAction(
    Map<String, dynamic> node,
    String action,
  ) async {
    final unlink = action == 'unlink';
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(unlink ? 'Unlink this worker?' : 'Clear worker history?'),
        content: Text(
          unlink
              ? 'The controller will stop assigning work to ${node['name'] ?? 'this worker'}. Media and persistent worker state are not deleted.'
              : 'Completed and failed rows and their logs will be cleared from ${node['name'] ?? 'this worker'}. Active jobs are preserved.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: Text(unlink ? 'Unlink' : 'Clear'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    await _run(
      () => widget.controller.nodeAction('${node['id'] ?? ''}', action),
      success: unlink ? 'Worker unlinked.' : 'Worker history cleared.',
    );
  }

  Future<void> _handleAction(Map<String, dynamic> node, String action) async {
    if (action == 'rename') return _rename(node);
    if (action == 'capacity') return _capacity(node);
    if (action == 'unlink' || action == 'clear_finished') {
      return _confirmNodeAction(node, action);
    }
    await _run(
      () => widget.controller.nodeAction('${node['id'] ?? ''}', action),
      success: 'Worker status refreshed.',
    );
  }

  @override
  Widget build(BuildContext context) {
    final local = asMap(widget.controller.nodes['local']);
    final nodes = asList(widget.controller.nodes['nodes']).map(asMap).toList();
    final all = [local, ...nodes].where((row) => row.isNotEmpty).toList();
    return _DetailScaffold(
      title: 'Encoding nodes',
      actions: [
        IconButton(
          onPressed: widget.controller.refreshAll,
          icon: const Icon(Icons.refresh_rounded),
        ),
      ],
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          for (var index = 0; index < all.length; index++)
            Padding(
              padding: const EdgeInsets.only(bottom: 11),
              child: _NodeCard(
                node: all[index],
                local: index == 0,
                canControl: widget.controller.canControl,
                onAction: (action) => _handleAction(all[index], action),
              ),
            ),
        ],
      ),
    );
  }
}

class _NodeCard extends StatelessWidget {
  const _NodeCard({
    required this.node,
    required this.local,
    required this.canControl,
    required this.onAction,
  });

  final Map<String, dynamic> node;
  final bool local;
  final bool canControl;
  final ValueChanged<String> onAction;

  @override
  Widget build(BuildContext context) {
    final online = node['online'] != false;
    final status = '${node['status'] ?? (online ? 'idle' : 'offline')}';
    final color = statusColor(online ? status : 'offline');
    final summary = asMap(node['summary']);
    final counts = asMap(summary['counts']);
    final capacity =
        (node['hardware_transcode_concurrency'] as num?)?.toInt() ?? 1;
    return SurfaceCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Stack(
                clipBehavior: Clip.none,
                children: [
                  DecoratedBox(
                    decoration: BoxDecoration(
                      color: ByteSqueezeColors.blue.withValues(alpha: .12),
                      borderRadius: BorderRadius.circular(15),
                    ),
                    child: const Padding(
                      padding: EdgeInsets.all(13),
                      child: Icon(
                        Icons.computer_rounded,
                        color: ByteSqueezeColors.cyan,
                        size: 28,
                      ),
                    ),
                  ),
                  Positioned(
                    right: -2,
                    bottom: -2,
                    child: Container(
                      width: 13,
                      height: 13,
                      decoration: BoxDecoration(
                        color: online
                            ? ByteSqueezeColors.mint
                            : ByteSqueezeColors.danger,
                        shape: BoxShape.circle,
                        border: Border.all(
                          color: ByteSqueezeColors.surface,
                          width: 2,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(width: 15),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '${node['name'] ?? (local ? 'TSD Main' : 'Worker')}',
                      style: const TextStyle(fontWeight: FontWeight.w700),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      local
                          ? 'Controller · protocol ${node['protocol_version'] ?? 2}'
                          : 'Linked worker · $capacity GPU slot${capacity == 1 ? '' : 's'} · protocol ${node['protocol_version'] ?? 2}',
                      style: const TextStyle(
                        color: ByteSqueezeColors.muted,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),
              StatusPill(label: online ? status : 'offline', color: color),
              if (!local)
                PopupMenuButton<String>(
                  enabled: canControl,
                  onSelected: onAction,
                  itemBuilder: (context) => const [
                    PopupMenuItem(
                      value: 'refresh',
                      child: ListTile(
                        leading: Icon(Icons.refresh_rounded),
                        title: Text('Refresh status'),
                      ),
                    ),
                    PopupMenuItem(
                      value: 'rename',
                      child: ListTile(
                        leading: Icon(Icons.drive_file_rename_outline_rounded),
                        title: Text('Rename worker'),
                      ),
                    ),
                    PopupMenuItem(
                      value: 'capacity',
                      child: ListTile(
                        leading: Icon(Icons.memory_rounded),
                        title: Text('GPU capacity'),
                      ),
                    ),
                    PopupMenuItem(
                      value: 'clear_finished',
                      child: ListTile(
                        leading: Icon(Icons.delete_sweep_outlined),
                        title: Text('Clear finished jobs'),
                      ),
                    ),
                    PopupMenuItem(
                      value: 'unlink',
                      child: ListTile(
                        leading: Icon(
                          Icons.link_off_rounded,
                          color: ByteSqueezeColors.danger,
                        ),
                        title: Text('Unlink worker'),
                      ),
                    ),
                  ],
                ),
            ],
          ),
          if (!local) ...[
            const SizedBox(height: 12),
            Text(
              '${node['url'] ?? ''}\n${counts['running'] ?? 0} running · ${counts['queued'] ?? 0} queued · ${counts['error'] ?? 0} errors',
              style: const TextStyle(
                color: ByteSqueezeColors.muted,
                fontSize: 12,
                height: 1.45,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class StoragePage extends StatelessWidget {
  const StoragePage({super.key, required this.controller});

  final AppController controller;

  @override
  Widget build(BuildContext context) {
    final summary = asMap(controller.storage['summary']);
    final rows = asList(controller.storage['encodes']).map(asMap).toList();
    return _DetailScaffold(
      title: 'Storage savings',
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          SurfaceCard(
            gradient: const LinearGradient(
              colors: [Color(0xFF0E5C58), Color(0xFF0A2235)],
            ),
            borderColor: const Color(0xFF18796F),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Icon(
                  Icons.savings_rounded,
                  color: ByteSqueezeColors.mint,
                  size: 34,
                ),
                const SizedBox(height: 22),
                Text(
                  formatBytes(summary['saved_bytes']),
                  style: Theme.of(context).textTheme.displaySmall,
                ),
                const Text(
                  'total space reclaimed',
                  style: TextStyle(color: ByteSqueezeColors.muted),
                ),
                const SizedBox(height: 10),
                Text(
                  '${summary['count'] ?? 0} completed encodes · ${formatDuration(summary['total_runtime_seconds'])} processing time',
                  style: const TextStyle(color: ByteSqueezeColors.mint),
                ),
              ],
            ),
          ),
          const SectionHeader(title: 'Recent savings'),
          if (rows.isEmpty)
            const EmptyState(
              icon: Icons.savings_outlined,
              title: 'No completed encodes',
              message:
                  'Savings appear after TSD verifies an output and records the result.',
            )
          else
            ...rows.map(
              (row) => Padding(
                padding: const EdgeInsets.only(bottom: 9),
                child: SurfaceCard(
                  padding: const EdgeInsets.all(15),
                  child: Row(
                    children: [
                      const CircleAvatar(
                        backgroundColor: Color(0x223FE1AE),
                        child: Icon(
                          Icons.arrow_downward_rounded,
                          color: ByteSqueezeColors.mint,
                        ),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              fileName(row['src']),
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                            const SizedBox(height: 3),
                            Text(
                              '${row['encoder'] ?? row['preset'] ?? 'encode'} · ${relativeTime(row['ts'])}',
                              style: const TextStyle(
                                color: ByteSqueezeColors.muted,
                                fontSize: 12,
                              ),
                            ),
                          ],
                        ),
                      ),
                      Text(
                        formatBytes(row['saved_bytes']),
                        style: const TextStyle(
                          color: ByteSqueezeColors.mint,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class EventsPage extends StatelessWidget {
  const EventsPage({super.key, required this.controller});

  final AppController controller;

  @override
  Widget build(BuildContext context) {
    final rows = asList(controller.events['events']).map(asMap).toList();
    return _DetailScaffold(
      title: 'Event timeline',
      child: ListView.builder(
        padding: const EdgeInsets.all(16),
        itemCount: rows.length,
        itemBuilder: (context, index) {
          final event = rows[index];
          final level = '${event['level'] ?? 'info'}';
          final color = level == 'error'
              ? ByteSqueezeColors.danger
              : (level == 'warn'
                  ? ByteSqueezeColors.amber
                  : ByteSqueezeColors.cyan);
          return Padding(
            padding: const EdgeInsets.only(bottom: 9),
            child: SurfaceCard(
              padding: const EdgeInsets.all(15),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Container(
                    width: 10,
                    height: 10,
                    margin: const EdgeInsets.only(top: 5),
                    decoration: BoxDecoration(
                      color: color,
                      shape: BoxShape.circle,
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          '${event['message'] ?? event['type'] ?? 'Server event'}',
                          style: const TextStyle(fontWeight: FontWeight.w600),
                        ),
                        const SizedBox(height: 5),
                        Text(
                          '${event['type'] ?? 'event'} · ${relativeTime(event['ts'])}',
                          style: const TextStyle(
                            color: ByteSqueezeColors.muted,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }
}

class ConnectionPage extends StatefulWidget {
  const ConnectionPage({super.key, required this.controller});

  final AppController controller;

  @override
  State<ConnectionPage> createState() => _ConnectionPageState();
}

class _ConnectionPageState extends State<ConnectionPage> {
  late final TextEditingController _primary;
  late final TextEditingController _fallback;
  bool _saving = false;

  AppController get controller => widget.controller;

  @override
  void initState() {
    super.initState();
    _primary = TextEditingController(text: controller.session?.baseUrl ?? '');
    _fallback = TextEditingController(
      text: controller.session?.fallbackBaseUrl ?? '',
    );
  }

  @override
  void dispose() {
    _primary.dispose();
    _fallback.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return _DetailScaffold(
      title: 'Connection & security',
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          SurfaceCard(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const StatusPill(
                  label: 'Bearer token pairing',
                  color: ByteSqueezeColors.mint,
                  icon: Icons.lock_rounded,
                ),
                const SizedBox(height: 18),
                _KeyValue(label: 'Server', value: controller.serverLabel),
                _KeyValue(
                  label: 'Current route',
                  value: controller.demoMode
                      ? 'Demo'
                      : controller.api.activeBaseUrl,
                ),
                _KeyValue(
                  label: 'Device',
                  value: controller.demoMode
                      ? 'Demo mode'
                      : (controller.session?.deviceName ?? 'ByteSqueeze'),
                ),
                _KeyValue(
                  label: 'Permission',
                  value:
                      controller.canControl ? 'Read and control' : 'Read only',
                ),
                _KeyValue(
                  label: 'Device ID',
                  value: controller.demoMode
                      ? 'demo'
                      : (controller.session?.deviceId ?? '—'),
                ),
              ],
            ),
          ),
          const SizedBox(height: 13),
          SurfaceCard(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  'Automatic address fallback',
                  style: Theme.of(context).textTheme.titleLarge,
                ),
                const SizedBox(height: 5),
                const Text(
                  'ByteSqueeze tries the current route, then your home and away addresses. It switches only when a connection fails.',
                  style: TextStyle(color: ByteSqueezeColors.muted),
                ),
                const SizedBox(height: 15),
                TextField(
                  controller: _primary,
                  enabled: !controller.demoMode,
                  keyboardType: TextInputType.url,
                  autocorrect: false,
                  decoration: const InputDecoration(
                    labelText: 'Home / primary address',
                    hintText: 'http://192.168.1.50:8080',
                    prefixIcon: Icon(Icons.home_outlined),
                  ),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _fallback,
                  enabled: !controller.demoMode,
                  keyboardType: TextInputType.url,
                  autocorrect: false,
                  decoration: const InputDecoration(
                    labelText: 'Away / Tailscale address (optional)',
                    hintText: 'http://100.x.x.x:8080',
                    prefixIcon: Icon(Icons.route_rounded),
                  ),
                ),
                const SizedBox(height: 15),
                FilledButton.icon(
                  onPressed: controller.demoMode || _saving ? null : _save,
                  icon: _saving
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.save_outlined),
                  label: const Text('Save connection addresses'),
                ),
              ],
            ),
          ),
          const SizedBox(height: 13),
          const SurfaceCard(
            borderColor: ByteSqueezeColors.amber,
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(Icons.security_rounded, color: ByteSqueezeColors.amber),
                SizedBox(width: 12),
                Expanded(
                  child: Text(
                    'Tokens are kept in platform secure storage. For remote access, put TSD behind an authenticated HTTPS reverse proxy instead of exposing port 8080 directly.',
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    try {
      await controller.updateServerAddresses(_primary.text, _fallback.text);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            'Connection addresses saved. Automatic fallback is active.',
          ),
        ),
      );
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('$error')));
      }
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }
}

class _KeyValue extends StatelessWidget {
  const _KeyValue({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 13),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: const TextStyle(
              color: ByteSqueezeColors.muted,
              fontSize: 12,
            ),
          ),
          const SizedBox(height: 3),
          SelectableText(
            value,
            style: const TextStyle(fontWeight: FontWeight.w600),
          ),
        ],
      ),
    );
  }
}
