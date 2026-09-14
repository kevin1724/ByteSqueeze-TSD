import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app_controller.dart';
import '../app_meta.dart';
import '../theme.dart';
import '../widgets/common.dart';
import 'automation_screen.dart';
import 'dashboard_screen.dart';
import 'jobs_screen.dart';
import 'library_screen.dart';
import 'more_screen.dart';

class AppShell extends StatefulWidget {
  const AppShell({super.key, required this.controller});

  final AppController controller;

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell> {
  static const _destinations = <_AppDestination>[
    _AppDestination(
      icon: Icons.space_dashboard_outlined,
      selectedIcon: Icons.space_dashboard_rounded,
      label: 'Overview',
      eyebrow: 'OPERATIONS',
      subtitle: 'Live health, savings, and quick controls',
    ),
    _AppDestination(
      icon: Icons.video_library_outlined,
      selectedIcon: Icons.video_library_rounded,
      label: 'Library',
      eyebrow: 'YOUR MEDIA',
      subtitle: 'Preview, fine-tune, and queue any scope',
    ),
    _AppDestination(
      icon: Icons.motion_photos_on_outlined,
      selectedIcon: Icons.motion_photos_on_rounded,
      label: 'Queue',
      eyebrow: 'ENCODING',
      subtitle: 'Running work, capacity, and history',
    ),
    _AppDestination(
      icon: Icons.auto_awesome_outlined,
      selectedIcon: Icons.auto_awesome_rounded,
      label: 'Autopilot',
      shortLabel: 'Automate',
      eyebrow: 'AUTOMATION',
      subtitle: 'Smart Presets, training, and guardrails',
    ),
    _AppDestination(
      icon: Icons.tune_outlined,
      selectedIcon: Icons.tune_rounded,
      label: 'Settings',
      shortLabel: 'More',
      eyebrow: 'SYSTEM',
      subtitle: 'Server, workers, app, and safety',
    ),
  ];

  Timer? _operationsTimer;
  bool _polling = false;
  late final PageController _pageController;

  AppController get controller => widget.controller;

  @override
  void initState() {
    super.initState();
    _pageController = PageController(initialPage: controller.selectedTab);
    if (!controller.demoMode) {
      _operationsTimer = Timer.periodic(
        const Duration(seconds: 15),
        (_) => _refreshOperations(),
      );
    }
  }

  @override
  void dispose() {
    _operationsTimer?.cancel();
    _pageController.dispose();
    super.dispose();
  }

  Widget _animatedV3Pages(BuildContext context, List<Widget> pages) {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_pageController.hasClients) return;
      final current =
          (_pageController.page ?? _pageController.initialPage).round();
      if (current == controller.selectedTab) return;
      final reduceMotion =
          MediaQuery.maybeOf(context)?.disableAnimations ?? false;
      if (reduceMotion) {
        _pageController.jumpToPage(controller.selectedTab);
      } else {
        _pageController.animateToPage(
          controller.selectedTab,
          duration: const Duration(milliseconds: 180),
          curve: Curves.easeOutCubic,
        );
      }
    });
    return PageView(
      controller: _pageController,
      physics: const NeverScrollableScrollPhysics(),
      children: pages,
    );
  }

  Future<void> _refreshOperations() async {
    if (_polling ||
        controller.busy ||
        controller.demoMode ||
        controller.session == null) {
      return;
    }
    _polling = true;
    try {
      await controller.refreshJobsAndDashboard();
    } catch (_) {
      // Background polling stays quiet; full-refresh errors use the banner.
    } finally {
      _polling = false;
    }
  }

  @override
  Widget build(BuildContext context) {
    final pages = <Widget>[
      DashboardScreen(controller: controller),
      LibraryScreen(controller: controller),
      JobsScreen(controller: controller),
      AutomationScreen(controller: controller),
      MoreScreen(controller: controller),
    ];
    final queue = asMap(controller.dashboard['queue']);
    final summary = asMap(queue['summary']);
    final activeJobs = asList(controller.dashboard['active_jobs']);
    final wide = MediaQuery.sizeOf(context).width >= 920;
    final shell = controller.useV3
        ? _buildV3(
            pages: pages,
            queue: queue,
            summary: summary,
            activeJobs: activeJobs,
            wide: wide,
          )
        : _buildClassic(
            pages: pages,
            queue: queue,
            summary: summary,
            activeJobs: activeJobs,
            wide: MediaQuery.sizeOf(context).width >= 880,
          );

    return CallbackShortcuts(
      bindings: <ShortcutActivator, VoidCallback>{
        const SingleActivator(LogicalKeyboardKey.keyK, control: true): () =>
            _openCommandCenter(context),
        const SingleActivator(LogicalKeyboardKey.keyK, meta: true): () =>
            _openCommandCenter(context),
        const SingleActivator(LogicalKeyboardKey.slash): () =>
            _openCommandCenter(context),
      },
      child: Focus(autofocus: true, child: shell),
    );
  }

  Widget _buildV3({
    required List<Widget> pages,
    required Map<String, dynamic> queue,
    required Map<String, dynamic> summary,
    required List<dynamic> activeJobs,
    required bool wide,
  }) {
    final paused = queue['paused'] == true;
    final workIsActive = summaryCount(summary, 'running') > 0;
    // Queue already has full live status, and Settings should never have
    // controls obscured by operational chrome.
    final showDock = workIsActive &&
        controller.selectedTab != 2 &&
        controller.selectedTab != 4;
    final page = _animatedV3Pages(context, pages);

    if (wide) {
      return Scaffold(
        body: ColoredBox(
          color: ByteSqueezeColors.canvas,
          child: SafeArea(
            child: Row(
              children: [
                _V3Sidebar(
                  controller: controller,
                  destinations: _destinations,
                  onCommand: () => _openCommandCenter(context),
                ),
                const VerticalDivider(width: 1),
                Expanded(
                  child: Column(
                    children: [
                      _WorkspaceBar(
                        destination: _destinations[controller.selectedTab],
                        controller: controller,
                        onCommand: () => _openCommandCenter(context),
                      ),
                      if (controller.error != null) _errorBanner(),
                      Expanded(
                        child: Stack(
                          children: [
                            Positioned.fill(child: page),
                            if (showDock)
                              Positioned(
                                left: 26,
                                right: 26,
                                bottom: 14,
                                child: Align(
                                  alignment: Alignment.bottomCenter,
                                  child: ConstrainedBox(
                                    constraints: const BoxConstraints(
                                      maxWidth: 720,
                                    ),
                                    child: OperationsDock(
                                      summary: summary,
                                      activeJobs: activeJobs,
                                      paused: paused,
                                      onTap: () => controller.selectTab(2),
                                    ),
                                  ),
                                ),
                              ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      );
    }

    return Scaffold(
      body: ColoredBox(
        color: ByteSqueezeColors.canvas,
        child: SafeArea(
          bottom: false,
          child: Column(
            children: [
              _MobileWorkspaceBar(
                destination: _destinations[controller.selectedTab],
                controller: controller,
                onCommand: () => _openCommandCenter(context),
              ),
              if (controller.error != null) _errorBanner(),
              Expanded(child: page),
            ],
          ),
        ),
      ),
      bottomNavigationBar: DecoratedBox(
        decoration: const BoxDecoration(
          color: Color(0xFF0B1017),
          border: Border(top: BorderSide(color: ByteSqueezeColors.subtleLine)),
        ),
        child: SafeArea(
          top: false,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (showDock)
                Padding(
                  padding: const EdgeInsets.fromLTRB(10, 8, 10, 0),
                  child: OperationsDock(
                    summary: summary,
                    activeJobs: activeJobs,
                    paused: paused,
                    onTap: () => controller.selectTab(2),
                  ),
                ),
              NavigationBar(
                selectedIndex: controller.selectedTab,
                onDestinationSelected: controller.selectTab,
                destinations: _destinations
                    .map(
                      (item) => NavigationDestination(
                        icon: Icon(item.icon),
                        selectedIcon: Icon(item.selectedIcon),
                        label: item.shortLabel ?? item.label,
                      ),
                    )
                    .toList(),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildClassic({
    required List<Widget> pages,
    required Map<String, dynamic> queue,
    required Map<String, dynamic> summary,
    required List<dynamic> activeJobs,
    required bool wide,
  }) {
    return Scaffold(
      extendBody: !wide,
      body: Stack(
        children: [
          Positioned.fill(
            child: DecoratedBox(
              decoration: const BoxDecoration(
                gradient: ByteSqueezeColors.backdrop,
              ),
              child: SafeArea(
                bottom: false,
                child: Column(
                  children: [
                    if (controller.error != null) _errorBanner(),
                    Expanded(
                      child: wide
                          ? Row(
                              children: [
                                NavigationRail(
                                  selectedIndex: controller.selectedTab,
                                  onDestinationSelected: controller.selectTab,
                                  labelType: NavigationRailLabelType.all,
                                  leading: const Padding(
                                    padding: EdgeInsets.only(bottom: 18),
                                    child: BrandMark(size: 42, showName: false),
                                  ),
                                  destinations: _destinations
                                      .map(
                                        (item) => NavigationRailDestination(
                                          icon: Icon(item.icon),
                                          selectedIcon: Icon(item.selectedIcon),
                                          label: Text(
                                            item.shortLabel ?? item.label,
                                          ),
                                        ),
                                      )
                                      .toList(),
                                ),
                                const VerticalDivider(width: 1),
                                Expanded(
                                  child: IndexedStack(
                                    index: controller.selectedTab,
                                    children: pages,
                                  ),
                                ),
                              ],
                            )
                          : IndexedStack(
                              index: controller.selectedTab,
                              children: pages,
                            ),
                    ),
                  ],
                ),
              ),
            ),
          ),
          if (controller.showSecondaryUi)
            Positioned(
              left: wide ? 112 : 12,
              right: wide ? 24 : 12,
              bottom: wide ? 14 : 88 + MediaQuery.paddingOf(context).bottom,
              child: Align(
                alignment: Alignment.bottomCenter,
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 720),
                  child: OperationsDock(
                    summary: summary,
                    activeJobs: activeJobs,
                    paused: queue['paused'] == true,
                    onTap: () => controller.selectTab(2),
                  ),
                ),
              ),
            ),
        ],
      ),
      bottomNavigationBar: wide
          ? null
          : SafeArea(
              minimum: const EdgeInsets.fromLTRB(12, 0, 12, 10),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(19),
                child: NavigationBar(
                  selectedIndex: controller.selectedTab,
                  onDestinationSelected: controller.selectTab,
                  destinations: _destinations
                      .map(
                        (item) => NavigationDestination(
                          icon: Icon(item.icon),
                          selectedIcon: Icon(item.selectedIcon),
                          label: item.shortLabel ?? item.label,
                        ),
                      )
                      .toList(),
                ),
              ),
            ),
    );
  }

  Widget _errorBanner() => MaterialBanner(
        backgroundColor: ByteSqueezeColors.danger.withValues(alpha: .10),
        leading: const Icon(
          Icons.cloud_off_rounded,
          color: ByteSqueezeColors.danger,
        ),
        content: Text(controller.error!),
        actions: [
          TextButton(
              onPressed: controller.refreshAll, child: const Text('Retry')),
        ],
      );

  Future<void> _openCommandCenter(BuildContext context) async {
    final queue = asMap(controller.dashboard['queue']);
    final paused = queue['paused'] == true;
    final actions = <_CommandAction>[
      for (var index = 0; index < _destinations.length; index++)
        _CommandAction(
          icon: _destinations[index].selectedIcon,
          title: 'Open ${_destinations[index].label}',
          subtitle: _destinations[index].subtitle,
          keywords: '${_destinations[index].eyebrow} navigation',
          run: () async => controller.selectTab(index),
        ),
      _CommandAction(
        icon: Icons.refresh_rounded,
        title: 'Refresh everything',
        subtitle: 'Sync server health, library, jobs, and Autopilot',
        keywords: 'reload sync update',
        run: controller.refreshAll,
      ),
      _CommandAction(
        icon: paused ? Icons.play_arrow_rounded : Icons.pause_rounded,
        title: paused ? 'Resume queue' : 'Pause queue',
        subtitle: paused
            ? 'Allow waiting work to start again'
            : 'Let running work finish without starting another job',
        keywords: 'encoding jobs control',
        requiresControl: true,
        run: () => controller.setQueuePaused(!paused),
      ),
      _CommandAction(
        icon: Icons.travel_explore_rounded,
        title: 'Scan media library',
        subtitle: 'Find new movies, shows, seasons, and episodes',
        keywords: 'refresh discover posters',
        requiresControl: true,
        run: controller.refreshLibrary,
      ),
      _CommandAction(
        icon: Icons.add_to_queue_rounded,
        title: 'Queue with Smart Presets',
        subtitle: 'Choose a movie, episode, season, or complete show',
        keywords: 'encode transcode new media',
        run: () async => controller.selectTab(1),
      ),
    ];

    final selected = await showModalBottomSheet<_CommandAction>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: false,
      builder: (context) =>
          _CommandCenter(actions: actions, canControl: controller.canControl),
    );
    if (selected == null || !mounted) return;
    try {
      await selected.run();
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(this.context)
          .showSnackBar(SnackBar(content: Text('$error')));
    }
  }
}

class _V3Sidebar extends StatelessWidget {
  const _V3Sidebar({
    required this.controller,
    required this.destinations,
    required this.onCommand,
  });

  final AppController controller;
  final List<_AppDestination> destinations;
  final VoidCallback onCommand;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 232,
      color: ByteSqueezeColors.shell.withValues(alpha: .94),
      padding: const EdgeInsets.fromLTRB(16, 18, 16, 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 8),
            child: Row(
              children: [
                const BrandMark(size: 36, showName: false),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    'ByteSqueeze',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 24),
          _CommandButton(onPressed: onCommand, expanded: true),
          const SizedBox(height: 22),
          const Padding(
            padding: EdgeInsets.symmetric(horizontal: 10),
            child: Text(
              'WORKSPACE',
              style: TextStyle(
                color: ByteSqueezeColors.muted,
                fontSize: 10,
                fontWeight: FontWeight.w800,
                letterSpacing: 1.35,
              ),
            ),
          ),
          const SizedBox(height: 8),
          for (var index = 0; index < destinations.length; index++)
            Padding(
              padding: const EdgeInsets.only(bottom: 5),
              child: _SidebarDestination(
                destination: destinations[index],
                selected: controller.selectedTab == index,
                onTap: () => controller.selectTab(index),
              ),
            ),
          const Spacer(),
          Container(
            padding: const EdgeInsets.all(13),
            decoration: BoxDecoration(
              color: ByteSqueezeColors.surface,
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: ByteSqueezeColors.subtleLine),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Row(
                  children: [
                    Icon(Icons.circle, color: ByteSqueezeColors.mint, size: 9),
                    SizedBox(width: 7),
                    Text(
                      'SERVER ONLINE',
                      style: TextStyle(
                        fontSize: 10,
                        fontWeight: FontWeight.w800,
                        letterSpacing: .8,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 7),
                Text(
                  controller.serverLabel,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: ByteSqueezeColors.muted,
                    fontSize: 11,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _SidebarDestination extends StatelessWidget {
  const _SidebarDestination({
    required this.destination,
    required this.selected,
    required this.onTap,
  });

  final _AppDestination destination;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: selected
          ? ByteSqueezeColors.cyan.withValues(alpha: .11)
          : Colors.transparent,
      borderRadius: BorderRadius.circular(14),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(14),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 11),
          child: Row(
            children: [
              Icon(
                selected ? destination.selectedIcon : destination.icon,
                size: 21,
                color:
                    selected ? ByteSqueezeColors.cyan : ByteSqueezeColors.muted,
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Text(
                  destination.label,
                  style: TextStyle(
                    color: selected
                        ? ByteSqueezeColors.ink
                        : ByteSqueezeColors.softInk,
                    fontWeight: selected ? FontWeight.w700 : FontWeight.w600,
                  ),
                ),
              ),
              if (selected)
                Container(
                  width: 5,
                  height: 5,
                  decoration: const BoxDecoration(
                    color: ByteSqueezeColors.cyan,
                    shape: BoxShape.circle,
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _WorkspaceBar extends StatelessWidget {
  const _WorkspaceBar({
    required this.destination,
    required this.controller,
    required this.onCommand,
  });

  final _AppDestination destination;
  final AppController controller;
  final VoidCallback onCommand;

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 58,
      padding: const EdgeInsets.symmetric(horizontal: 26),
      decoration: const BoxDecoration(
        color: Color(0xB80A0E14),
        border: Border(bottom: BorderSide(color: ByteSqueezeColors.subtleLine)),
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  destination.label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                if (controller.statsForNerds)
                  Text(
                    destination.subtitle,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: ByteSqueezeColors.muted,
                      fontSize: 10.5,
                    ),
                  ),
              ],
            ),
          ),
          if (controller.showSecondaryUi) ...[
            _CommandButton(onPressed: onCommand, expanded: true),
            const SizedBox(width: 9),
            IconButton.filledTonal(
              tooltip: 'Refresh all',
              onPressed: controller.busy ? null : controller.refreshAll,
              icon: controller.busy
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.refresh_rounded),
            ),
          ],
        ],
      ),
    );
  }
}

class _MobileWorkspaceBar extends StatelessWidget {
  const _MobileWorkspaceBar({
    required this.destination,
    required this.controller,
    required this.onCommand,
  });

  final _AppDestination destination;
  final AppController controller;
  final VoidCallback onCommand;

  @override
  Widget build(BuildContext context) {
    final queue = asMap(controller.dashboard['queue']);
    final summary = asMap(queue['summary']);
    final running = summaryCount(summary, 'running');
    final nodes = asMap(controller.dashboard['nodes']);
    final online = (nodes['online'] as num?)?.toInt() ?? 0;
    return Container(
      height: 52,
      padding: const EdgeInsets.fromLTRB(15, 4, 7, 4),
      decoration: const BoxDecoration(
        color: Color(0xD90A0E14),
        border: Border(bottom: BorderSide(color: ByteSqueezeColors.subtleLine)),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              destination.shortLabel ?? destination.label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w800),
            ),
          ),
          Tooltip(
            message: running > 0
                ? '$running encoding now'
                : '$online worker${online == 1 ? '' : 's'} online',
            child: Semantics(
              label: running > 0
                  ? '$running encoding now'
                  : '$online workers online',
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 8),
                child: Icon(
                  Icons.circle,
                  size: 8,
                  color: running > 0
                      ? ByteSqueezeColors.cyan
                      : ByteSqueezeColors.mint,
                ),
              ),
            ),
          ),
          IconButton(
            tooltip: 'Command center',
            onPressed: onCommand,
            icon: const Icon(Icons.search_rounded),
          ),
          IconButton(
            tooltip: 'Queue media',
            onPressed: () => controller.selectTab(1),
            icon: const Icon(Icons.add_rounded),
          ),
          if (controller.busy)
            const Padding(
              padding: EdgeInsets.only(right: 8),
              child: SizedBox(
                width: 17,
                height: 17,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
            )
          else
            const SizedBox(width: 8),
        ],
      ),
    );
  }
}

class _CommandButton extends StatelessWidget {
  const _CommandButton({required this.onPressed, required this.expanded});

  final VoidCallback onPressed;
  final bool expanded;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final showShortcut = expanded &&
            (!constraints.hasBoundedWidth || constraints.maxWidth >= 250);
        return OutlinedButton.icon(
          onPressed: onPressed,
          icon: const Icon(Icons.search_rounded, size: 19),
          label: showShortcut
              ? const Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text('Command center'),
                    SizedBox(width: 20),
                    Text(
                      '⌘ K',
                      style: TextStyle(
                        color: ByteSqueezeColors.muted,
                        fontSize: 10,
                      ),
                    ),
                  ],
                )
              : Text(expanded ? 'Command center' : 'Search'),
          style: OutlinedButton.styleFrom(
            foregroundColor: ByteSqueezeColors.softInk,
            backgroundColor: ByteSqueezeColors.surface.withValues(alpha: .7),
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          ),
        );
      },
    );
  }
}

class _CommandCenter extends StatefulWidget {
  const _CommandCenter({required this.actions, required this.canControl});

  final List<_CommandAction> actions;
  final bool canControl;

  @override
  State<_CommandCenter> createState() => _CommandCenterState();
}

class _CommandCenterState extends State<_CommandCenter> {
  final _search = TextEditingController();

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final query = _search.text.trim().toLowerCase();
    final actions = widget.actions.where((action) {
      if (query.isEmpty) return true;
      return '${action.title} ${action.subtitle} ${action.keywords}'
          .toLowerCase()
          .contains(query);
    }).toList();
    return FractionallySizedBox(
      heightFactor: .86,
      child: DecoratedBox(
        decoration: const BoxDecoration(
          gradient: ByteSqueezeColors.commandBackdrop,
          borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
        ),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Center(
                child: Container(
                  width: 42,
                  height: 4,
                  decoration: BoxDecoration(
                    color: ByteSqueezeColors.line,
                    borderRadius: BorderRadius.circular(99),
                  ),
                ),
              ),
              const SizedBox(height: 20),
              Row(
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Command center',
                          style: Theme.of(context).textTheme.headlineSmall,
                        ),
                        const Text(
                          'Go anywhere or run a safe server action',
                          style: TextStyle(
                            color: ByteSqueezeColors.muted,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                  ),
                  const StatusPill(
                    label: appReleaseLabel,
                    color: ByteSqueezeColors.violet,
                    icon: Icons.auto_awesome_rounded,
                  ),
                ],
              ),
              const SizedBox(height: 16),
              TextField(
                controller: _search,
                autofocus: true,
                onChanged: (_) => setState(() {}),
                decoration: const InputDecoration(
                  hintText: 'Search pages and actions',
                  prefixIcon: Icon(Icons.search_rounded),
                ),
              ),
              const SizedBox(height: 12),
              Expanded(
                child: actions.isEmpty
                    ? const EmptyState(
                        icon: Icons.search_off_rounded,
                        title: 'No matching command',
                        message: 'Try library, queue, refresh, or Autopilot.',
                      )
                    : ListView.separated(
                        itemCount: actions.length,
                        separatorBuilder: (_, __) => const SizedBox(height: 7),
                        itemBuilder: (context, index) {
                          final action = actions[index];
                          final enabled =
                              !action.requiresControl || widget.canControl;
                          return Material(
                            color: ByteSqueezeColors.surface,
                            borderRadius: BorderRadius.circular(16),
                            child: InkWell(
                              onTap: enabled
                                  ? () => Navigator.pop(context, action)
                                  : null,
                              borderRadius: BorderRadius.circular(16),
                              child: Padding(
                                padding: const EdgeInsets.all(13),
                                child: Row(
                                  children: [
                                    Container(
                                      padding: const EdgeInsets.all(10),
                                      decoration: BoxDecoration(
                                        color: ByteSqueezeColors.cyan
                                            .withValues(alpha: .1),
                                        borderRadius: BorderRadius.circular(12),
                                      ),
                                      child: Icon(
                                        action.icon,
                                        color: enabled
                                            ? ByteSqueezeColors.cyan
                                            : ByteSqueezeColors.muted,
                                      ),
                                    ),
                                    const SizedBox(width: 12),
                                    Expanded(
                                      child: Column(
                                        crossAxisAlignment:
                                            CrossAxisAlignment.start,
                                        children: [
                                          Text(
                                            action.title,
                                            style: TextStyle(
                                              color: enabled
                                                  ? ByteSqueezeColors.ink
                                                  : ByteSqueezeColors.muted,
                                              fontWeight: FontWeight.w700,
                                            ),
                                          ),
                                          const SizedBox(height: 2),
                                          Text(
                                            enabled
                                                ? action.subtitle
                                                : 'Control access is required',
                                            maxLines: 2,
                                            overflow: TextOverflow.ellipsis,
                                            style: const TextStyle(
                                              color: ByteSqueezeColors.muted,
                                              fontSize: 11.5,
                                            ),
                                          ),
                                        ],
                                      ),
                                    ),
                                    const Icon(
                                      Icons.arrow_forward_rounded,
                                      color: ByteSqueezeColors.muted,
                                      size: 18,
                                    ),
                                  ],
                                ),
                              ),
                            ),
                          );
                        },
                      ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _AppDestination {
  const _AppDestination({
    required this.icon,
    required this.selectedIcon,
    required this.label,
    required this.eyebrow,
    required this.subtitle,
    this.shortLabel,
  });

  final IconData icon;
  final IconData selectedIcon;
  final String label;
  final String eyebrow;
  final String subtitle;
  final String? shortLabel;
}

class _CommandAction {
  const _CommandAction({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.keywords,
    required this.run,
    this.requiresControl = false,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final String keywords;
  final Future<void> Function() run;
  final bool requiresControl;
}
