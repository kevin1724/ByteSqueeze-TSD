import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../app_controller.dart';
import '../theme.dart';
import '../widgets/audio_optimizer_sheet.dart';
import '../widgets/common.dart';
import 'size_wizard_screen.dart';

class LibraryScreen extends StatefulWidget {
  const LibraryScreen({super.key, required this.controller});

  final AppController controller;

  @override
  State<LibraryScreen> createState() => _LibraryScreenState();
}

class _LibraryScreenState extends State<LibraryScreen> {
  final _search = TextEditingController();
  String _section = 'movies';
  String _quickFilter = 'all';
  String _sort = 'recommended';

  bool get _shows => _section == 'shows';
  bool get _dvr => _section == 'dvr';

  bool _isShowItem(Map<String, dynamic> item) =>
      item['type'] == 'show' || item['type'] == 'dvr_show';

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final all = asList(widget.controller.library[_section])
        .map(asMap)
        .toList();
    final query = _search.text.trim().toLowerCase();
    final items = all.where((item) {
      final files = asList(item['files'])
          .map((row) => '${asMap(row)['path'] ?? ''}')
          .join(' ');
      final matchesSearch = query.isEmpty ||
          '${item['title'] ?? ''} ${item['year'] ?? ''} $files'
              .toLowerCase()
              .contains(query);
      return matchesSearch && _matchesQuickFilter(item);
    }).toList()
      ..sort(_compareItems);
    final stats = asMap(widget.controller.library['stats']);
    final configured = widget.controller.library['configured'] != false;
    final tmdbConfigured = widget.controller.library['tmdb_configured'] == true;

    return RefreshIndicator(
      onRefresh: widget.controller.canControl
          ? widget.controller.refreshLibrary
          : widget.controller.refreshAll,
      child: CustomScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        slivers: [
          SliverToBoxAdapter(
            child: PageInsets(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  CompactPageHeader(
                    title: 'Library',
                    summary:
                        '${(stats['movies'] as num?)?.toInt() ?? 0} movies · ${(stats['shows'] as num?)?.toInt() ?? 0} shows · ${(stats['dvr'] as num?)?.toInt() ?? 0} DVR',
                    trailing: widget.controller.showSecondaryUi
                        ? IconButton(
                            tooltip: 'Refresh library on server',
                            onPressed: widget.controller.canControl &&
                                    !widget.controller.busy
                                ? () => _run(widget.controller.refreshLibrary)
                                : null,
                            icon: const Icon(Icons.sync_rounded),
                          )
                        : null,
                  ),
                  if (widget.controller.statsForNerds) ...[
                    const SizedBox(height: 12),
                    Row(
                      children: [
                        StatusPill(
                          label: tmdbConfigured
                              ? 'TMDb preferred'
                              : 'No-key artwork',
                          color: tmdbConfigured
                              ? ByteSqueezeColors.mint
                              : ByteSqueezeColors.blue,
                          icon: tmdbConfigured
                              ? Icons.verified_outlined
                              : Icons.image_search_rounded,
                        ),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            tmdbConfigured
                                ? 'TMDb posters are used first; other artwork fills any gaps.'
                                : 'Local, TVmaze, and Apple artwork work without an API key.',
                            style: const TextStyle(
                              color: ByteSqueezeColors.muted,
                              fontSize: 11.5,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ],
                  TextField(
                    controller: _search,
                    onChanged: (_) => setState(() {}),
                    decoration: InputDecoration(
                      hintText: _shows
                          ? 'Search shows'
                          : _dvr
                              ? 'Search DVR recordings'
                              : 'Search movies',
                      prefixIcon: const Icon(Icons.search_rounded),
                      suffixIcon: _search.text.isEmpty
                          ? null
                          : IconButton(
                              onPressed: () {
                                _search.clear();
                                setState(() {});
                              },
                              icon: const Icon(Icons.close_rounded),
                            ),
                    ),
                  ),
                  const SizedBox(height: 10),
                  Row(
                    children: [
                      Expanded(
                        child: SegmentedButton<String>(
                          segments: const [
                            ButtonSegment(value: 'movies', label: Text('Movies')),
                            ButtonSegment(value: 'shows', label: Text('Shows')),
                            ButtonSegment(value: 'dvr', label: Text('DVR')),
                          ],
                          selected: {_section},
                          onSelectionChanged: (value) =>
                              setState(() => _section = value.first),
                          showSelectedIcon: false,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Expanded(
                        child: SingleChildScrollView(
                          scrollDirection: Axis.horizontal,
                          child: Row(
                            children: [
                              for (final filter in [
                                ('all', 'All', Icons.apps_rounded),
                                (
                                  'savings',
                                  'Best savings',
                                  Icons.compress_rounded,
                                ),
                                ('hdr', 'HDR', Icons.hdr_on_rounded),
                                (
                                  'transcoded',
                                  'Transcoded',
                                  Icons.video_file_rounded,
                                ),
                                if (_shows)
                                  (
                                    'tracked',
                                    'Tracked',
                                    Icons.notifications_active_outlined,
                                  ),
                              ]) ...[
                                FilterChip(
                                  selected: _quickFilter == filter.$1,
                                  onSelected: (_) =>
                                      setState(() => _quickFilter = filter.$1),
                                  avatar: Icon(filter.$3, size: 17),
                                  label: Text(filter.$2),
                                ),
                                const SizedBox(width: 7),
                              ],
                            ],
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      PopupMenuButton<String>(
                        tooltip: 'Sort library',
                        initialValue: _sort,
                        onSelected: (value) => setState(() => _sort = value),
                        itemBuilder: (context) => const [
                          PopupMenuItem(
                            value: 'recommended',
                            child: Text('Recommended savings'),
                          ),
                          PopupMenuItem(
                            value: 'size',
                            child: Text('Largest source'),
                          ),
                          PopupMenuItem(
                            value: 'title',
                            child: Text('Title A–Z'),
                          ),
                        ],
                        child: DecoratedBox(
                          decoration: BoxDecoration(
                            color: ByteSqueezeColors.surface,
                            borderRadius: BorderRadius.circular(14),
                            border: Border.all(color: ByteSqueezeColors.line),
                          ),
                          child: const Padding(
                            padding: EdgeInsets.all(12),
                            child: Icon(Icons.sort_rounded),
                          ),
                        ),
                      ),
                    ],
                  ),
                  if (!configured) ...[
                    const SizedBox(height: 14),
                    const SurfaceCard(
                      borderColor: ByteSqueezeColors.amber,
                      child: Row(
                        children: [
                          Icon(
                            Icons.folder_off_outlined,
                            color: ByteSqueezeColors.amber,
                          ),
                          SizedBox(width: 12),
                          Expanded(
                            child: Text(
                              'Map Movies, Shows, and DVR folders in the ByteSqueeze web settings, then refresh here.',
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                  if (widget.controller.showSecondaryUi &&
                      query.isEmpty &&
                      items.isNotEmpty) ...[
                    SectionHeader(
                      title: _shows
                          ? 'Tracked shows'
                          : _dvr
                              ? 'Recent DVR recordings'
                              : 'Recently added',
                      subtitle: _shows
                          ? 'Favorites with release and download monitoring'
                          : 'Latest files discovered on mapped drives',
                    ),
                    _MediaRail(
                      items: (_shows
                              ? all
                                  .where(
                                    (item) => item['tracked'] == true,
                                  )
                                  .toList()
                              : asList(
                                  asMap(
                                    widget.controller.library['catalog'],
                                  )['recently_added'],
                                )
                                  .map(asMap)
                                  .where((item) => _dvr
                                      ? '${item['type'] ?? ''}'
                                          .startsWith('dvr_')
                                      : item['type'] == 'movie')
                                  .toList())
                          .take(12)
                          .toList(),
                      isShow: _shows,
                      onTap: _openDetails,
                    ),
                    const SectionHeader(
                      title: 'Complete catalog',
                      subtitle: 'Every title currently found on mapped drives',
                    ),
                  ],
                  const SizedBox(height: 16),
                  if (widget.controller.statsForNerds && items.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 10),
                      child: Text(
                        '${items.length} ${_shows ? 'show${items.length == 1 ? '' : 's'}' : _dvr ? 'DVR recording${items.length == 1 ? '' : 's'}' : 'movie${items.length == 1 ? '' : 's'}'} shown',
                        style: const TextStyle(
                          color: ByteSqueezeColors.muted,
                          fontWeight: FontWeight.w700,
                          fontSize: 12,
                        ),
                      ),
                    ),
                  if (items.isEmpty)
                    EmptyState(
                      icon: _shows
                          ? Icons.tv_off_outlined
                          : Icons.movie_filter_outlined,
                      title:
                          query.isEmpty ? 'Nothing scanned yet' : 'No matches',
                      message: query.isEmpty
                          ? 'Run a library refresh after mapping media folders in TSD.'
                          : 'Try another title or year.',
                    ),
                ],
              ),
            ),
          ),
          if (items.isNotEmpty)
            SliverLayoutBuilder(
              builder: (context, constraints) {
                final viewport = constraints.crossAxisExtent;
                final baseInset = viewport < 600 ? 14.0 : 24.0;
                final centeredInset = math
                    .max(baseInset, (viewport - 1280) / 2 + baseInset)
                    .toDouble();
                final usable = viewport - centeredInset * 2;
                final columns = usable >= 1100
                    ? 7
                    : (usable >= 820 ? 5 : (usable >= 540 ? 4 : 2));
                return SliverPadding(
                  padding: EdgeInsets.fromLTRB(
                    centeredInset,
                    0,
                    centeredInset,
                    24,
                  ),
                  sliver: SliverGrid.builder(
                    gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
                      crossAxisCount: columns,
                      mainAxisSpacing: 12,
                      crossAxisSpacing: 10,
                      childAspectRatio: .61,
                    ),
                    itemCount: items.length,
                    itemBuilder: (context, index) => _MediaTile(
                      item: items[index],
                      isShow: _isShowItem(items[index]),
                      onTap: () => _openDetails(items[index]),
                    ),
                  ),
                );
              },
            ),
        ],
      ),
    );
  }

  Future<void> _openDetails(Map<String, dynamic> item) async {
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: ByteSqueezeColors.navy,
      showDragHandle: true,
      builder: (context) => _MediaDetails(
        controller: widget.controller,
        item: item,
        isShow: _isShowItem(item),
      ),
    );
    if (mounted) setState(() {});
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

  bool _matchesQuickFilter(Map<String, dynamic> item) {
    if (_quickFilter == 'all') return true;
    if (_quickFilter == 'savings') {
      final prediction = asMap(item['prediction']);
      return prediction['available'] == true &&
          ((prediction['savings_percent'] as num?)?.toDouble() ?? 0) >= 10;
    }
    if (_quickFilter == 'hdr') {
      return item['is_hdr'] == true ||
          asList(item['files']).map(asMap).any((row) => row['is_hdr'] == true);
    }
    if (_quickFilter == 'tracked') {
      return _shows && item['tracked'] == true;
    }
    if (_quickFilter == 'transcoded') {
      return item['transcoded'] == true ||
          item['has_transcoded'] == true ||
          asList(item['files'])
              .map(asMap)
              .any((row) => row['transcoded'] == true);
    }
    return true;
  }

  int _compareItems(Map<String, dynamic> a, Map<String, dynamic> b) {
    final aTitle = '${a['title'] ?? ''}'.toLowerCase();
    final bTitle = '${b['title'] ?? ''}'.toLowerCase();
    if (_sort == 'title') return aTitle.compareTo(bTitle);
    int bytes(Map<String, dynamic> item) =>
        ((item['total_size_bytes'] ?? item['size_bytes']) as num?)?.toInt() ??
        0;
    if (_sort == 'size') {
      final result = bytes(b).compareTo(bytes(a));
      return result != 0 ? result : aTitle.compareTo(bTitle);
    }
    int saved(Map<String, dynamic> item) =>
        (asMap(item['prediction'])['estimated_saved_bytes'] as num?)?.toInt() ??
        (bytes(item) * .35).round();
    final result = saved(b).compareTo(saved(a));
    return result != 0 ? result : aTitle.compareTo(bTitle);
  }
}

class _MediaRail extends StatelessWidget {
  const _MediaRail({
    required this.items,
    required this.isShow,
    required this.onTap,
  });

  final List<Map<String, dynamic>> items;
  final bool isShow;
  final ValueChanged<Map<String, dynamic>> onTap;

  @override
  Widget build(BuildContext context) {
    if (items.isEmpty) {
      return SurfaceCard(
        child: Text(
          isShow
              ? 'Track shows to keep favorites and upcoming episodes here.'
              : 'Newly discovered movies appear here after the next scan.',
          style: const TextStyle(color: ByteSqueezeColors.muted),
        ),
      );
    }
    return SizedBox(
      height: 222,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        itemCount: items.length,
        separatorBuilder: (_, __) => const SizedBox(width: 11),
        itemBuilder: (context, index) {
          final item = items[index];
          return SizedBox(
            width: 126,
            child: InkWell(
              onTap: () => onTap(item),
              borderRadius: BorderRadius.circular(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(child: PosterArt(item: item, borderRadius: 16)),
                  const SizedBox(height: 7),
                  Text(
                    '${item['title'] ?? 'Unknown'}',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                  Text(
                    '${item['year'] ?? ''}',
                    style: const TextStyle(
                      color: ByteSqueezeColors.muted,
                      fontSize: 11.5,
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

class _MediaTile extends StatelessWidget {
  const _MediaTile({
    required this.item,
    required this.isShow,
    required this.onTap,
  });

  final Map<String, dynamic> item;
  final bool isShow;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final prediction = asMap(item['prediction']);
    final savings = (prediction['savings_percent'] as num?)?.round();
    final transcoded =
        item['transcoded'] == true || item['has_transcoded'] == true;
    return Material(
      color: ByteSqueezeColors.surface,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: const BorderSide(color: ByteSqueezeColors.subtleLine),
      ),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Stack(
                fit: StackFit.expand,
                children: [
                  PosterArt(item: item, borderRadius: 0),
                  const DecoratedBox(
                    decoration: BoxDecoration(
                      gradient: LinearGradient(
                        begin: Alignment.center,
                        end: Alignment.bottomCenter,
                        colors: [Colors.transparent, Color(0x88030A15)],
                      ),
                    ),
                  ),
                  Positioned(
                    top: 8,
                    right: 8,
                    child: transcoded
                        ? const StatusPill(
                            label: 'Transcoded',
                            color: ByteSqueezeColors.mint,
                            icon: Icons.check_circle_outline_rounded,
                          )
                        : isShow && item['tracked'] == true
                            ? const StatusPill(
                                label: 'Tracked',
                                color: ByteSqueezeColors.mint,
                                icon: Icons.notifications_active_outlined,
                              )
                            : savings != null
                                ? StatusPill(
                                    label: '$savings% save',
                                    color: ByteSqueezeColors.cyan,
                                  )
                                : const SizedBox.shrink(),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(11, 9, 11, 10),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    '${item['title'] ?? 'Unknown'}',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w800),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    isShow
                        ? '${item['season_count'] ?? 0} seasons · ${item['episode_count'] ?? 0} episodes'
                        : '${item['year'] ?? 'Unknown year'} · ${formatBytes(item['size_bytes'] ?? item['total_size_bytes'])}',
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
          ],
        ),
      ),
    );
  }
}

class _MediaDetails extends StatefulWidget {
  const _MediaDetails({
    required this.controller,
    required this.item,
    required this.isShow,
  });

  final AppController controller;
  final Map<String, dynamic> item;
  final bool isShow;

  @override
  State<_MediaDetails> createState() => _MediaDetailsState();
}

class _MediaDetailsState extends State<_MediaDetails> {
  bool _working = false;
  bool _previewWorking = false;
  String _queueTarget = 'local:';
  Map<String, dynamic> _smartTuning = <String, dynamic>{};
  Map<String, dynamic> _preview = <String, dynamic>{};

  List<String> get _paths {
    if (widget.isShow) {
      return asList(widget.item['files'])
          .map((row) => '${asMap(row)['path'] ?? ''}')
          .where((path) => path.isNotEmpty)
          .toList();
    }
    final values = asList(widget.item['paths'])
        .map((value) => '$value')
        .where((path) => path.isNotEmpty)
        .toList();
    final path = '${widget.item['path'] ?? ''}';
    if (values.isEmpty && path.isNotEmpty) values.add(path);
    return values;
  }

  List<String> get _videoPaths {
    if (widget.isShow) {
      return asList(widget.item['files'])
          .map(asMap)
          .where((row) =>
              row['transcoded'] != true && row['queue_ready'] != false)
          .map((row) => '${row['path'] ?? ''}')
          .where((path) => path.isNotEmpty)
          .toList();
    }
    return widget.item['transcoded'] == true ? <String>[] : _paths;
  }

  Future<void> _queue(
    String preset, {
    List<String>? paths,
    String? scopeLabel,
  }) async {
    final queuePaths = paths ?? _videoPaths;
    if (queuePaths.isEmpty) {
      final waitingForRecording = widget.isShow &&
          asList(widget.item['files']).map(asMap).any((row) =>
              row['transcoded'] != true && row['queue_ready'] == false);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            waitingForRecording
                ? 'Plex is still writing this recording. It will become queueable after it stops changing.'
                : 'This video is already transcoded. Use Optimize audio only.',
          ),
        ),
      );
      return;
    }
    setState(() => _working = true);
    try {
      final parts = _queueTarget.split(':');
      final mode = parts.first;
      final nodeId = parts.length > 1 ? parts.sublist(1).join(':') : '';
      await widget.controller.queuePaths(
        queuePaths,
        preset: preset,
        mode: mode,
        nodeId: nodeId,
        smartTuning: preset == 'smart' ? _smartTuning : null,
      );
      if (!mounted) return;
      final messenger = ScaffoldMessenger.of(context);
      Navigator.pop(context);
      messenger.showSnackBar(
        SnackBar(
          content: Text(
            '${scopeLabel ?? widget.item['title']} queued for ${_targetLabel()}.',
          ),
        ),
      );
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('$error')));
    } finally {
      if (mounted) setState(() => _working = false);
    }
  }

  Future<void> _editSmartTuning() async {
    final rawProfile = widget.controller.smartPresets['profile'];
    final safetyProfile = rawProfile is Map
        ? Map<String, dynamic>.from(rawProfile)
        : <String, dynamic>{};
    final tuning = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      backgroundColor: ByteSqueezeColors.navy,
      builder: (context) =>
          _SmartTuneSheet(initial: _smartTuning, profile: safetyProfile),
    );
    if (tuning == null || !mounted) return;
    setState(() {
      _smartTuning = tuning;
      _preview = <String, dynamic>{};
    });
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(
        content: Text('Smart guardrails applied to this queue only.'),
      ),
    );
  }

  Future<void> _generatePreview({List<String>? paths}) async {
    final previewPaths = paths ?? _paths;
    if (previewPaths.isEmpty || _previewWorking) return;
    setState(() {
      _previewWorking = true;
      _preview = <String, dynamic>{
        'state': 'queued',
        'progress': 0,
        'message': 'Starting a real Smart comparison…',
      };
    });
    try {
      final result = await widget.controller.generateLibraryPreview(
        previewPaths.first,
        smartTuning: _smartTuning,
        onProgress: (value) {
          if (mounted) setState(() => _preview = value);
        },
      );
      if (mounted) setState(() => _preview = result);
    } catch (error) {
      if (!mounted) return;
      setState(
        () =>
            _preview = <String, dynamic>{'state': 'error', 'message': '$error'},
      );
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('$error')));
    } finally {
      if (mounted) setState(() => _previewWorking = false);
    }
  }

  Future<void> _openSizeWizard(Map<String, dynamic> file) async {
    final path = '${file['path'] ?? ''}';
    if (path.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('This library item has no source path.')),
      );
      return;
    }
    final episode = file['episode'];
    final title = episode == null
        ? '${widget.item['title'] ?? fileName(path)}'
        : '${widget.item['title'] ?? 'Episode'} · S${file['season'] ?? 0}E$episode';
    await Navigator.push<bool>(
      context,
      MaterialPageRoute(
        builder: (_) => SizeWizardScreen(
          controller: widget.controller,
          path: path,
          title: title,
        ),
      ),
    );
  }

  Future<void> _openAudioOptimization(Map<String, dynamic> file) async {
    final path = '${file['path'] ?? ''}';
    if (path.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('This library item has no source path.')),
      );
      return;
    }
    final queued = await showAudioOptimizerSheet(
      context,
      controller: widget.controller,
      job: <String, dynamic>{'src': path, 'out_path': path},
    );
    if (queued == true && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
            content: Text('Audio-only job queued. Video will be copied.')),
      );
    }
  }

  Map<int, List<Map<String, dynamic>>> _seasonGroups(List<dynamic> values) {
    final groups = <int, List<Map<String, dynamic>>>{};
    for (final value in values) {
      final file = asMap(value);
      final season = (file['season'] as num?)?.toInt() ?? 0;
      groups.putIfAbsent(season, () => <Map<String, dynamic>>[]).add(file);
    }
    for (final rows in groups.values) {
      rows.sort(
        (a, b) => ((a['episode'] as num?)?.toInt() ?? 0).compareTo(
          (b['episode'] as num?)?.toInt() ?? 0,
        ),
      );
    }
    return Map.fromEntries(
      groups.entries.toList()..sort((a, b) => a.key.compareTo(b.key)),
    );
  }

  List<String> _filePaths(Iterable<Map<String, dynamic>> files) => files
      .map((row) => '${row['path'] ?? ''}')
      .where((path) => path.isNotEmpty)
      .toList();

  String _targetLabel() {
    if (_queueTarget == 'local:') return 'this server';
    if (_queueTarget == 'available:') {
      return 'the next free nodes as capacity opens';
    }
    if (_queueTarget == 'best:') return 'one best node for this selection';
    final id = _queueTarget.substring('node:'.length);
    for (final value in asList(widget.controller.nodes['nodes'])) {
      final node = asMap(value);
      if ('${node['id'] ?? ''}' == id) {
        return '${node['name'] ?? 'the selected node'}';
      }
    }
    return 'the selected node';
  }

  Future<void> _track(bool value) async {
    setState(() => widget.item['tracked'] = value);
    try {
      await widget.controller.trackShow(widget.item, value);
    } catch (error) {
      if (!mounted) return;
      setState(() => widget.item['tracked'] = !value);
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('$error')));
    }
  }

  @override
  Widget build(BuildContext context) {
    final files = asList(widget.item['files']);
    final seasons = widget.isShow
        ? _seasonGroups(files)
        : <int, List<Map<String, dynamic>>>{};
    final allTranscoded = widget.isShow
        ? _paths.isNotEmpty &&
            files.map(asMap).every((row) => row['transcoded'] == true)
        : widget.item['transcoded'] == true;
    final posterSource =
        '${widget.item['poster_source'] ?? widget.item['metadata_source'] ?? widget.item['source'] ?? ''}'
            .toLowerCase();
    final artworkLabel = posterSource == 'tmdb'
        ? 'Artwork: TMDb'
        : (posterSource == 'tvmaze'
            ? 'Artwork: TVmaze'
            : (posterSource == 'apple'
                ? 'Artwork: Apple'
                : (posterSource == 'local' ? 'Artwork: local' : '')));
    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: .96,
      minChildSize: .64,
      maxChildSize: 1,
      builder: (context, scrollController) => ListView(
        controller: scrollController,
        padding: const EdgeInsets.fromLTRB(20, 6, 20, 40),
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SizedBox(
                width: 118,
                height: 177,
                child: PosterArt(item: widget.item, borderRadius: 16),
              ),
              const SizedBox(width: 18),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '${widget.item['title'] ?? 'Unknown'}',
                      style: Theme.of(context).textTheme.headlineSmall,
                    ),
                    const SizedBox(height: 7),
                    Text(
                      '${widget.item['year'] ?? ''}',
                      style: const TextStyle(color: ByteSqueezeColors.muted),
                    ),
                    const SizedBox(height: 14),
                    Wrap(
                      spacing: 7,
                      runSpacing: 7,
                      children: [
                        if (widget.isShow)
                          StatusPill(
                            label:
                                '${widget.item['episode_count'] ?? files.length} episodes',
                            icon: Icons.tv_rounded,
                          ),
                        if (!widget.isShow)
                          StatusPill(
                            label: formatBytes(
                              widget.item['size_bytes'] ??
                                  widget.item['total_size_bytes'],
                            ),
                            icon: Icons.storage_rounded,
                          ),
                        if (widget.item['quality'] != null)
                          StatusPill(
                            label: '${widget.item['quality']}',
                            color: ByteSqueezeColors.mint,
                          ),
                        if (widget.item['transcoded'] == true ||
                            widget.item['has_transcoded'] == true)
                          const StatusPill(
                            label: 'Transcoded',
                            color: ByteSqueezeColors.mint,
                            icon: Icons.check_circle_outline_rounded,
                          ),
                        if (artworkLabel.isNotEmpty &&
                            widget.controller.statsForNerds)
                          StatusPill(
                            label: artworkLabel,
                            color: posterSource == 'tmdb'
                                ? ByteSqueezeColors.mint
                                : ByteSqueezeColors.blue,
                            icon: Icons.image_outlined,
                          ),
                      ],
                    ),
                  ],
                ),
              ),
            ],
          ),
          if (widget.isShow) ...[
            const SizedBox(height: 18),
            SurfaceCard(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
              child: SwitchListTile.adaptive(
                value: widget.item['tracked'] == true,
                onChanged: widget.controller.canControl ? _track : null,
                title: const Text(
                  'Track new episodes',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                subtitle: const Text(
                  'Add release dates to Calendar and watch mapped drives for new files.',
                ),
                secondary: const Icon(
                  Icons.notifications_active_outlined,
                  color: ByteSqueezeColors.cyan,
                ),
              ),
            ),
            if (widget.item['tracked'] == true)
              SurfaceCard(
                padding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 4,
                ),
                child: Column(
                  children: [
                    SwitchListTile.adaptive(
                      contentPadding: EdgeInsets.zero,
                      value: widget.item['monitor_releases'] != false,
                      onChanged: widget.controller.canControl
                          ? (value) async {
                              setState(
                                () => widget.item['monitor_releases'] = value,
                              );
                              await widget.controller.trackShow(
                                widget.item,
                                true,
                              );
                            }
                          : null,
                      title: const Text('Upcoming episode calendar'),
                      subtitle: const Text(
                        'Show known release dates from TVmaze.',
                      ),
                      secondary: const Icon(Icons.calendar_month_outlined),
                    ),
                    const Divider(height: 1),
                    SwitchListTile.adaptive(
                      contentPadding: EdgeInsets.zero,
                      value: widget.item['auto_queue_downloads'] != false,
                      onChanged: widget.controller.canControl
                          ? (value) async {
                              setState(
                                () =>
                                    widget.item['auto_queue_downloads'] = value,
                              );
                              await widget.controller.trackShow(
                                widget.item,
                                true,
                              );
                            }
                          : null,
                      title: const Text('Auto-queue finished downloads'),
                      subtitle: const Text(
                        'Wait until a new file stops changing, then queue it.',
                      ),
                      secondary: const Icon(Icons.download_done_rounded),
                    ),
                  ],
                ),
              ),
          ],
          SectionHeader(
            title: allTranscoded ? 'Audio optimization' : 'Smart plan',
            subtitle: allTranscoded
                ? 'The video is locked to COPY. Only selected audio tracks can change.'
                : 'Every episode gets its own probe, HDR decision, quality floor, and preset snapshot',
          ),
          SurfaceCard(
            borderColor: ByteSqueezeColors.cyan.withValues(alpha: .34),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    DecoratedBox(
                      decoration: BoxDecoration(
                        color: ByteSqueezeColors.cyan.withValues(alpha: .12),
                        shape: BoxShape.circle,
                      ),
                      child: const Padding(
                        padding: EdgeInsets.all(10),
                        child: Icon(
                          Icons.auto_awesome_rounded,
                          color: ByteSqueezeColors.cyan,
                        ),
                      ),
                    ),
                    const SizedBox(width: 11),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            widget.isShow
                                ? '${_paths.length} episodes in full-show scope'
                                : 'One movie in queue scope',
                            style: const TextStyle(fontWeight: FontWeight.w800),
                          ),
                          const SizedBox(height: 3),
                          Text(
                            _smartTuning.isEmpty
                                ? 'Using your learned Smart profile'
                                : '${_smartTuning.length} temporary guardrails applied',
                            style: const TextStyle(
                              color: ByteSqueezeColors.muted,
                              fontSize: 12,
                            ),
                          ),
                        ],
                      ),
                    ),
                    if (!allTranscoded)
                      TextButton.icon(
                        onPressed: widget.controller.canControl && !_working
                            ? _editSmartTuning
                            : null,
                        icon: const Icon(Icons.tune_rounded, size: 18),
                        label: const Text('Tune'),
                      ),
                  ],
                ),
                const SizedBox(height: 12),
                if (!allTranscoded)
                  FilledButton.tonalIcon(
                    onPressed: _paths.isEmpty
                        ? null
                        : () => _openSizeWizard(
                              files.isNotEmpty
                                  ? asMap(files.first)
                                  : <String, dynamic>{'path': _paths.first},
                            ),
                    icon: const Icon(Icons.straighten_rounded),
                    label: Text(
                      widget.isShow
                          ? 'Size Wizard for first episode'
                          : 'Open in Size Wizard',
                    ),
                  ),
                if (!allTranscoded) const SizedBox(height: 9),
                OutlinedButton.icon(
                  onPressed: _paths.isEmpty
                      ? null
                      : () => _openAudioOptimization(
                            files.isNotEmpty
                                ? asMap(files.first)
                                : <String, dynamic>{'path': _paths.first},
                          ),
                  icon: const Icon(Icons.graphic_eq_rounded),
                  label: Text(
                    widget.isShow
                        ? 'Optimize audio for first episode'
                        : 'Optimize audio only',
                  ),
                ),
                if (!allTranscoded) const SizedBox(height: 9),
                if (!allTranscoded)
                  OutlinedButton.icon(
                    onPressed: widget.controller.canControl &&
                            !_previewWorking &&
                            _paths.isNotEmpty
                        ? _generatePreview
                        : null,
                    icon: _previewWorking
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.compare_rounded),
                    label: Text(
                      _preview.isEmpty
                          ? (widget.isShow
                              ? 'Preview first episode’s Smart encode'
                              : 'Preview real Smart encode')
                          : 'Refresh Smart preview',
                    ),
                  ),
              ],
            ),
          ),
          if (_preview.isNotEmpty)
            _LibraryPreviewCard(preview: _preview, working: _previewWorking),
          if (!allTranscoded)
            const SectionHeader(
              title: 'Queue destination',
              subtitle:
                  'Distribute each file automatically or lock this selection to one node',
            ),
          if (!allTranscoded)
            DropdownButtonFormField<String>(
              initialValue: _queueTarget,
              isExpanded: true,
              decoration: const InputDecoration(
                labelText: 'Encoding node',
                prefixIcon: Icon(Icons.hub_outlined),
              ),
              items: [
                const DropdownMenuItem(
                  value: 'local:',
                  child: Text('This server (local)'),
                ),
                const DropdownMenuItem(
                  value: 'available:',
                  child: Text('Next available · distribute files'),
                ),
                if (asList(widget.controller.nodes['nodes'])
                    .map(asMap)
                    .any((row) => row['online'] == true))
                  const DropdownMenuItem(
                    value: 'best:',
                    child: Text('Best node · keep selection together'),
                  ),
                ...asList(widget.controller.nodes['nodes'])
                    .map(asMap)
                    .where((row) => row['online'] == true)
                    .map(
                      (row) => DropdownMenuItem(
                        value: 'node:${row['id']}',
                        child: Text('${row['name'] ?? 'Worker node'}'),
                      ),
                    ),
              ],
              onChanged: _working
                  ? null
                  : (value) => setState(() => _queueTarget = value ?? 'local:'),
            ),
          if (!allTranscoded) const SizedBox(height: 12),
          if (!allTranscoded)
            Row(
              children: [
                Expanded(
                  child: FilledButton.icon(
                    onPressed: widget.controller.canControl && !_working
                        ? () => _queue('smart')
                        : null,
                    icon: _working
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.auto_awesome_rounded),
                    label: Text(
                      widget.isShow
                          ? 'Smart Queue Full Show'
                          : 'Smart Queue Movie',
                    ),
                  ),
                ),
                const SizedBox(width: 9),
                PopupMenuButton<String>(
                  enabled: widget.controller.canControl && !_working,
                  tooltip: 'Other server presets',
                  onSelected: _queue,
                  itemBuilder: (context) => const [
                    PopupMenuItem(
                        value: 'auto', child: Text('Automatic preset')),
                    PopupMenuItem(value: '1080', child: Text('1080p preset')),
                    PopupMenuItem(value: '4k', child: Text('4K preset')),
                  ],
                  child: const DecoratedBox(
                    decoration: BoxDecoration(
                      color: ByteSqueezeColors.raised,
                      borderRadius: BorderRadius.all(Radius.circular(14)),
                    ),
                    child: Padding(
                      padding: EdgeInsets.all(14),
                      child: Icon(Icons.more_horiz_rounded),
                    ),
                  ),
                ),
              ],
            ),
          if (widget.isShow && seasons.isNotEmpty) ...[
            const SectionHeader(
              title: 'Seasons',
              subtitle:
                  'Preview one episode; queue planning remains independent for every episode',
            ),
            ...seasons.entries.map((entry) {
              final season = entry.key;
              final rows = entry.value;
              final paths = _filePaths(
                rows.where((row) =>
                    row['transcoded'] != true && row['queue_ready'] != false),
              );
              final totalBytes = rows.fold<int>(
                0,
                (sum, row) => sum + ((row['size_bytes'] as num?)?.toInt() ?? 0),
              );
              return Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: SurfaceCard(
                  padding: EdgeInsets.zero,
                  child: ExpansionTile(
                    tilePadding: const EdgeInsets.fromLTRB(14, 5, 8, 5),
                    childrenPadding: const EdgeInsets.only(bottom: 8),
                    leading: CircleAvatar(
                      backgroundColor: ByteSqueezeColors.blue.withValues(
                        alpha: .14,
                      ),
                      child: Text(
                        season > 0 ? '$season' : '—',
                        style: const TextStyle(
                          color: ByteSqueezeColors.cyan,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                    ),
                    title: Text(
                      season > 0 ? 'Season $season' : 'Specials',
                      style: const TextStyle(fontWeight: FontWeight.w800),
                    ),
                    subtitle: Text(
                      '${rows.length} episode${rows.length == 1 ? '' : 's'} · ${formatBytes(totalBytes)}',
                      style: const TextStyle(
                        color: ByteSqueezeColors.muted,
                        fontSize: 12,
                      ),
                    ),
                    trailing: Wrap(
                      spacing: 3,
                      children: [
                        IconButton(
                          tooltip: 'Preview the first episode in this season',
                          onPressed: widget.controller.canControl &&
                                  !_previewWorking &&
                                  paths.isNotEmpty
                              ? () => _generatePreview(paths: paths)
                              : null,
                          icon: const Icon(Icons.compare_rounded),
                        ),
                        IconButton.filledTonal(
                          tooltip: 'Smart Queue this season',
                          onPressed: widget.controller.canControl &&
                                  !_working &&
                                  paths.isNotEmpty
                              ? () => _queue(
                                    'smart',
                                    paths: paths,
                                    scopeLabel: season > 0
                                        ? '${widget.item['title']} Season $season'
                                        : '${widget.item['title']} Specials',
                                  )
                              : null,
                          icon: const Icon(Icons.add_to_queue_rounded),
                        ),
                      ],
                    ),
                    children: rows.take(100).map((file) {
                      return ListTile(
                        dense: true,
                        leading: Text(
                          'E${file['episode'] ?? '—'}',
                          style: const TextStyle(
                            color: ByteSqueezeColors.cyan,
                            fontWeight: FontWeight.w800,
                          ),
                        ),
                        title: Text(
                          fileName(file['path']),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                        subtitle: Text(
                          file['queue_ready'] == false
                              ? 'Recording in progress · ${formatBytes(file['size_bytes'])}'
                              : ((file['version_count'] as num?)?.toInt() ?? 1) > 1
                                  ? '${file['version_count']} versions · ${formatBytes(file['size_bytes'])}'
                                  : formatBytes(file['size_bytes']),
                          style: const TextStyle(
                            color: ByteSqueezeColors.muted,
                          ),
                        ),
                        trailing: Wrap(
                          children: [
                            IconButton(
                              tooltip: 'Optimize episode audio only',
                              onPressed: file['queue_ready'] == false
                                  ? null
                                  : () => _openAudioOptimization(file),
                              icon: const Icon(Icons.graphic_eq_rounded),
                            ),
                            if (file['transcoded'] != true)
                              IconButton(
                                tooltip: file['queue_ready'] == false
                                    ? 'Wait for Plex to finish this recording'
                                    : 'Open this episode in Size Wizard',
                                onPressed: file['queue_ready'] == false
                                    ? null
                                    : () => _openSizeWizard(file),
                                icon: const Icon(Icons.straighten_rounded),
                              ),
                          ],
                        ),
                      );
                    }).toList(),
                  ),
                ),
              );
            }),
          ] else if (files.isNotEmpty) ...[
            SectionHeader(
              title: 'Files',
              subtitle: '${files.length} media files',
            ),
            SurfaceCard(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Column(
                children: files.take(100).map((row) {
                  final file = asMap(row);
                  return ListTile(
                    leading: const Icon(
                      Icons.movie_outlined,
                      color: ByteSqueezeColors.cyan,
                    ),
                    title: Text(
                      fileName(file['path']),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    subtitle: Text(
                      formatBytes(file['size_bytes']),
                      style: const TextStyle(color: ByteSqueezeColors.muted),
                    ),
                    trailing: Wrap(
                      children: [
                        IconButton(
                          tooltip: 'Optimize audio only',
                          onPressed: () => _openAudioOptimization(file),
                          icon: const Icon(Icons.graphic_eq_rounded),
                        ),
                        IconButton(
                          tooltip: 'Open in Size Wizard',
                          onPressed: () => _openSizeWizard(file),
                          icon: const Icon(Icons.straighten_rounded),
                        ),
                      ],
                    ),
                  );
                }).toList(),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _LibraryPreviewCard extends StatelessWidget {
  const _LibraryPreviewCard({required this.preview, required this.working});

  final Map<String, dynamic> preview;
  final bool working;

  @override
  Widget build(BuildContext context) {
    final result = asMap(preview['result']);
    final progress = (((preview['progress'] as num?)?.toDouble() ?? 0) / 100)
        .clamp(0, 1)
        .toDouble();
    final oldFrame = '${result['old_b64'] ?? ''}';
    final newFrame = '${result['new_b64'] ?? ''}';
    final ready = preview['state'] == 'done';
    final encoder = '${result['encoder_label'] ?? result['encoder'] ?? ''}';
    final dimensions =
        result['out_width'] != null && result['out_height'] != null
            ? '${result['out_width']}×${result['out_height']}'
            : '';
    return Padding(
      padding: const EdgeInsets.only(top: 10),
      child: SurfaceCard(
        borderColor: ready ? ByteSqueezeColors.mint : ByteSqueezeColors.line,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Icon(
                  ready
                      ? Icons.check_circle_rounded
                      : Icons.movie_filter_rounded,
                  color:
                      ready ? ByteSqueezeColors.mint : ByteSqueezeColors.cyan,
                ),
                const SizedBox(width: 9),
                Expanded(
                  child: Text(
                    ready ? 'Smart preview ready' : 'Building Smart preview',
                    style: const TextStyle(fontWeight: FontWeight.w800),
                  ),
                ),
                if (ready && (encoder.isNotEmpty || dimensions.isNotEmpty))
                  StatusPill(
                    label: [
                      encoder,
                      dimensions,
                    ].where((value) => value.isNotEmpty).join(' · '),
                    color: ByteSqueezeColors.mint,
                  ),
              ],
            ),
            const SizedBox(height: 9),
            LinearProgressIndicator(value: ready ? 1.0 : progress),
            const SizedBox(height: 8),
            Text(
              '${preview['message'] ?? (working ? 'Encoding a short matched sample…' : 'Preview finished.')}',
              style: const TextStyle(
                color: ByteSqueezeColors.muted,
                fontSize: 12,
              ),
            ),
            if (ready && oldFrame.isNotEmpty && newFrame.isNotEmpty) ...[
              const SizedBox(height: 12),
              LayoutBuilder(
                builder: (context, constraints) {
                  final frames = [
                    _LibraryPreviewFrame(label: 'Original', value: oldFrame),
                    _LibraryPreviewFrame(
                      label: 'Smart proposal',
                      value: newFrame,
                    ),
                  ];
                  if (constraints.maxWidth >= 520) {
                    return Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Expanded(child: frames[0]),
                        const SizedBox(width: 9),
                        Expanded(child: frames[1]),
                      ],
                    );
                  }
                  return Column(
                    children: [frames[0], const SizedBox(height: 9), frames[1]],
                  );
                },
              ),
              const SizedBox(height: 8),
              const Text(
                'Frames are captured at the same moment so faces, text, motion detail, and dark areas are easy to compare.',
                style: TextStyle(
                  color: ByteSqueezeColors.muted,
                  fontSize: 11.5,
                ),
              ),
            ] else if (ready) ...[
              const SizedBox(height: 8),
              const Text(
                'Demo preview metadata is ready. Connect to your server to render matched source and proposal frames.',
                style: TextStyle(
                  color: ByteSqueezeColors.muted,
                  fontSize: 11.5,
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _LibraryPreviewFrame extends StatelessWidget {
  const _LibraryPreviewFrame({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    Uint8List? bytes;
    try {
      bytes = base64Decode(value);
    } catch (_) {}
    final previewBytes = bytes;
    final image = previewBytes == null
        ? const SizedBox(
            height: 120,
            child: Center(child: Icon(Icons.broken_image_outlined)),
          )
        : Image.memory(
            previewBytes,
            fit: BoxFit.contain,
            gaplessPlayback: true,
          );
    return Material(
      color: const Color(0xFF020713),
      borderRadius: BorderRadius.circular(14),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: previewBytes == null
            ? null
            : () => _openPreview(context, previewBytes),
        child: DecoratedBox(
          decoration: BoxDecoration(
            border: Border.all(color: ByteSqueezeColors.line),
            borderRadius: BorderRadius.circular(14),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(10, 8, 8, 7),
                child: Row(
                  children: [
                    Expanded(
                      child: Text(
                        label.toUpperCase(),
                        style: const TextStyle(
                          color: ByteSqueezeColors.muted,
                          fontSize: 10,
                          fontWeight: FontWeight.w900,
                          letterSpacing: 1,
                        ),
                      ),
                    ),
                    if (previewBytes != null)
                      const Icon(
                        Icons.zoom_out_map_rounded,
                        color: ByteSqueezeColors.muted,
                        size: 15,
                      ),
                  ],
                ),
              ),
              image,
            ],
          ),
        ),
      ),
    );
  }

  void _openPreview(BuildContext context, Uint8List bytes) {
    showDialog<void>(
      context: context,
      builder: (context) => Dialog(
        insetPadding: const EdgeInsets.all(12),
        backgroundColor: const Color(0xFF020407),
        child: Stack(
          children: [
            Positioned.fill(
              child: InteractiveViewer(
                minScale: .8,
                maxScale: 5,
                child: Center(child: Image.memory(bytes, fit: BoxFit.contain)),
              ),
            ),
            Positioned(
              top: 8,
              left: 12,
              child: StatusPill(label: label, color: ByteSqueezeColors.cyan),
            ),
            Positioned(
              top: 5,
              right: 5,
              child: IconButton.filled(
                onPressed: () => Navigator.pop(context),
                icon: const Icon(Icons.close_rounded),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _SmartTuneSheet extends StatefulWidget {
  const _SmartTuneSheet({required this.initial, required this.profile});

  final Map<String, dynamic> initial;
  final Map<String, dynamic> profile;

  @override
  State<_SmartTuneSheet> createState() => _SmartTuneSheetState();
}

class _SmartTuneSheetState extends State<_SmartTuneSheet> {
  late String _goal;
  late String _resolution;
  late String _hardware;
  late String _compatibility;
  late String _audio;
  late String _subtitles;
  late double _targetScale;

  bool get _resolutionLocked => widget.profile['never_downscale'] != false;
  bool get _audioLocked => widget.profile['never_transcode_audio'] != false;
  bool get _subtitlesLocked =>
      widget.profile['keep_all_subtitle_languages'] != false;

  @override
  void initState() {
    super.initState();
    _goal = '${widget.initial['goal'] ?? 'learned'}';
    _resolution = '${widget.initial['resolution_mode'] ?? 'learned'}';
    _hardware = '${widget.initial['hardware'] ?? 'learned'}';
    _compatibility = '${widget.initial['compatibility'] ?? 'learned'}';
    _audio = '${widget.initial['audio_strategy'] ?? 'learned'}';
    _subtitles = '${widget.initial['subtitle_mode'] ?? 'learned'}';
    _targetScale = ((widget.initial['target_scale'] as num?)?.toDouble() ?? 1)
        .clamp(.7, 1.3)
        .toDouble();
    if (_resolutionLocked) _resolution = 'keep';
    if (_audioLocked) _audio = 'copy';
    if (_subtitlesLocked) _subtitles = 'all';
  }

  Map<String, dynamic> _value() {
    final value = <String, dynamic>{'target_scale': _targetScale};
    void add(String key, String candidate) {
      if (candidate != 'learned') value[key] = candidate;
    }

    add('goal', _goal);
    add('resolution_mode', _resolution);
    add('hardware', _hardware);
    add('compatibility', _compatibility);
    add('audio_strategy', _audio);
    add('subtitle_mode', _subtitles);
    return value;
  }

  void _reset() {
    setState(() {
      _goal = 'learned';
      _resolution = _resolutionLocked ? 'keep' : 'learned';
      _hardware = 'learned';
      _compatibility = 'learned';
      _audio = _audioLocked ? 'copy' : 'learned';
      _subtitles = _subtitlesLocked ? 'all' : 'learned';
      _targetScale = 1;
    });
  }

  @override
  Widget build(BuildContext context) {
    return FractionallySizedBox(
      heightFactor: .94,
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 4, 20, 14),
            child: Row(
              children: [
                const Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Fine-tune Smart Queue',
                        style: TextStyle(
                          fontSize: 21,
                          fontWeight: FontWeight.w900,
                        ),
                      ),
                      SizedBox(height: 3),
                      Text(
                        'Guardrails for this queue only',
                        style: TextStyle(color: ByteSqueezeColors.muted),
                      ),
                    ],
                  ),
                ),
                TextButton(onPressed: _reset, child: const Text('Reset')),
              ],
            ),
          ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
              children: [
                SurfaceCard(
                  borderColor: ByteSqueezeColors.cyan,
                  child: Text(
                    [
                      if (_resolutionLocked) 'source resolution',
                      if (widget.profile['keep_black_bars'] != false)
                        'black bars',
                      if (widget.profile['keep_aspect_ratio'] != false)
                        'aspect ratio',
                      if (_audioLocked) 'audio passthrough',
                      if (widget.profile['keep_all_audio_languages'] != false)
                        'all audio languages',
                      if (_subtitlesLocked) 'all subtitles',
                    ].isEmpty
                        ? 'No saved hard protections are enabled. These choices guide this queue without changing your learned profile.'
                        : 'Saved protections are locked: ${[
                            if (_resolutionLocked) 'source resolution',
                            if (widget.profile['keep_black_bars'] != false)
                              'black bars',
                            if (widget.profile['keep_aspect_ratio'] != false)
                              'aspect ratio',
                            if (_audioLocked) 'audio passthrough',
                            if (widget.profile['keep_all_audio_languages'] !=
                                false)
                              'all audio languages',
                            if (_subtitlesLocked) 'all subtitles'
                          ].join(', ')}.',
                    style: const TextStyle(
                      color: ByteSqueezeColors.muted,
                      fontSize: 12.5,
                    ),
                  ),
                ),
                _tuningDropdown(
                  label: 'Priority',
                  value: _goal,
                  values: const {
                    'learned': 'Use learned profile',
                    'balanced': 'Balanced',
                    'quality': 'Protect detail',
                    'small': 'Save more space',
                    'speed': 'Finish faster',
                    'archive': 'Archive quality',
                  },
                  onChanged: (value) => setState(() => _goal = value),
                ),
                _tuningDropdown(
                  label: 'Resolution guardrail',
                  value: _resolution,
                  values: const {
                    'learned': 'Smart automatic',
                    'keep': 'Keep source resolution',
                    '2160': 'Cap at 2160p',
                    '1440': 'Cap at 1440p',
                    '1080': 'Cap at 1080p',
                    '720': 'Cap at 720p',
                  },
                  onChanged: _resolutionLocked
                      ? null
                      : (value) => setState(() => _resolution = value),
                ),
                _tuningDropdown(
                  label: 'Encoder',
                  value: _hardware,
                  values: const {
                    'learned': 'Use learned profile',
                    'auto': 'Best available',
                    'software': 'Software quality',
                    'qsv': 'Intel Quick Sync',
                    'nvenc': 'NVIDIA NVENC',
                  },
                  onChanged: (value) => setState(() => _hardware = value),
                ),
                _tuningDropdown(
                  label: 'Playback compatibility',
                  value: _compatibility,
                  values: const {
                    'learned': 'Use learned profile',
                    'broad': 'Broad / H.264 friendly',
                    'modern': 'Modern / H.265',
                    'maximum': 'Maximum compression',
                  },
                  onChanged: (value) => setState(() => _compatibility = value),
                ),
                _tuningDropdown(
                  label: 'Audio',
                  value: _audio,
                  values: const {
                    'learned': 'Use learned profile',
                    'copy': 'Copy original tracks',
                    'eac3_surround': 'E-AC3 surround',
                  },
                  onChanged: _audioLocked
                      ? null
                      : (value) => setState(() => _audio = value),
                ),
                _tuningDropdown(
                  label: 'Subtitles',
                  value: _subtitles,
                  values: const {
                    'learned': 'Use learned profile',
                    'all': 'Keep all matching',
                    'first': 'First matching track',
                    'none': 'No subtitles',
                  },
                  onChanged: _subtitlesLocked
                      ? null
                      : (value) => setState(() => _subtitles = value),
                ),
                const SizedBox(height: 14),
                SurfaceCard(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Row(
                        children: [
                          const Expanded(
                            child: Text(
                              'Size vs. detail',
                              style: TextStyle(fontWeight: FontWeight.w800),
                            ),
                          ),
                          Text(
                            _targetScale == 1
                                ? 'Learned target'
                                : (_targetScale < 1
                                    ? '${((1 - _targetScale) * 100).round()}% smaller'
                                    : '${((_targetScale - 1) * 100).round()}% more detail'),
                            style: const TextStyle(
                              color: ByteSqueezeColors.cyan,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ],
                      ),
                      Slider(
                        value: _targetScale,
                        min: .7,
                        max: 1.3,
                        divisions: 12,
                        onChanged: (value) =>
                            setState(() => _targetScale = value),
                      ),
                      const Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          Text(
                            'Smaller',
                            style: TextStyle(
                              color: ByteSqueezeColors.muted,
                              fontSize: 11,
                            ),
                          ),
                          Text(
                            'More detail',
                            style: TextStyle(
                              color: ByteSqueezeColors.muted,
                              fontSize: 11,
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          SafeArea(
            top: false,
            minimum: const EdgeInsets.fromLTRB(20, 10, 20, 14),
            child: FilledButton.icon(
              onPressed: () => Navigator.pop(context, _value()),
              icon: const Icon(Icons.check_rounded),
              label: const Text('Use these guardrails'),
            ),
          ),
        ],
      ),
    );
  }

  Widget _tuningDropdown({
    required String label,
    required String value,
    required Map<String, String> values,
    required ValueChanged<String>? onChanged,
  }) {
    return Padding(
      padding: const EdgeInsets.only(top: 11),
      child: DropdownButtonFormField<String>(
        key: ValueKey('$label:$value'),
        initialValue: value,
        decoration: InputDecoration(labelText: label),
        items: values.entries
            .map(
              (entry) =>
                  DropdownMenuItem(value: entry.key, child: Text(entry.value)),
            )
            .toList(),
        onChanged: onChanged == null
            ? null
            : (next) {
                if (next != null) onChanged(next);
              },
      ),
    );
  }
}
