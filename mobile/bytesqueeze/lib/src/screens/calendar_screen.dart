import 'package:flutter/material.dart';

import '../app_controller.dart';
import '../theme.dart';
import '../widgets/common.dart';

class CalendarScreen extends StatefulWidget {
  const CalendarScreen({super.key, required this.controller});

  final AppController controller;

  @override
  State<CalendarScreen> createState() => _CalendarScreenState();
}

class _CalendarScreenState extends State<CalendarScreen> {
  bool _trackedOnly = false;
  int _windowDays = 30;

  DateTime get _today {
    final now = DateTime.now();
    return DateTime(now.year, now.month, now.day);
  }

  @override
  Widget build(BuildContext context) {
    final today = _today;
    final end = today.add(Duration(days: _windowDays));
    final allDays = asList(widget.controller.calendar['days']).map(asMap);
    final days = allDays
        .map((day) {
          final date = DateTime.tryParse('${day['date'] ?? ''}');
          final inWindow =
              date == null || (!date.isBefore(today) && !date.isAfter(end));
          final episodes = asList(day['episodes'])
              .map(asMap)
              .where((episode) => !_trackedOnly || episode['tracked'] == true)
              .toList();
          return {...day, 'episodes': episodes, '_in_window': inWindow};
        })
        .where((day) =>
            day['_in_window'] == true && asList(day['episodes']).isNotEmpty)
        .toList()
      ..sort((left, right) =>
          '${left['date'] ?? ''}'.compareTo('${right['date'] ?? ''}'));
    final count = days.fold<int>(
      0,
      (total, day) => total + asList(day['episodes']).length,
    );
    final trackedCount = days.fold<int>(
      0,
      (total, day) =>
          total +
          asList(day['episodes'])
              .map(asMap)
              .where((episode) => episode['tracked'] == true)
              .length,
    );
    final showCount = days
        .expand((day) => asList(day['episodes']).map(asMap))
        .map((episode) => '${episode['show_title'] ?? ''}'.trim())
        .where((title) => title.isNotEmpty)
        .toSet()
        .length;
    final nextDate =
        days.isEmpty ? null : DateTime.tryParse('${days.first['date'] ?? ''}');

    return Scaffold(
      appBar: AppBar(title: const Text('Release calendar')),
      body: RefreshIndicator(
        onRefresh: widget.controller.refreshAll,
        child: CustomScrollView(
          physics: const AlwaysScrollableScrollPhysics(),
          slivers: [
            SliverToBoxAdapter(
              child: PageInsets(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Row(
                      children: [
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              const Text(
                                'RELEASE RADAR',
                                style: TextStyle(
                                  color: ByteSqueezeColors.cyan,
                                  fontSize: 11,
                                  fontWeight: FontWeight.w800,
                                  letterSpacing: 1.6,
                                ),
                              ),
                              const SizedBox(height: 5),
                              Text(
                                'What’s coming next',
                                style:
                                    Theme.of(context).textTheme.headlineLarge,
                              ),
                              const SizedBox(height: 4),
                              const Text(
                                'A cleaner view of releases ByteSqueeze is watching for.',
                                style:
                                    TextStyle(color: ByteSqueezeColors.muted),
                              ),
                            ],
                          ),
                        ),
                        IconButton.filledTonal(
                          tooltip: 'Refresh release data',
                          onPressed: widget.controller.busy
                              ? null
                              : widget.controller.refreshAll,
                          icon: const Icon(Icons.sync_rounded),
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    _RadarHero(
                      episodeCount: count,
                      trackedCount: trackedCount,
                      showCount: showCount,
                      nextDate: nextDate,
                      today: today,
                    ),
                    const SizedBox(height: 16),
                    SegmentedButton<bool>(
                      segments: const [
                        ButtonSegment(
                          value: false,
                          icon: Icon(Icons.explore_outlined),
                          label: Text('All releases'),
                        ),
                        ButtonSegment(
                          value: true,
                          icon: Icon(Icons.notifications_active_outlined),
                          label: Text('Tracked'),
                        ),
                      ],
                      selected: {_trackedOnly},
                      showSelectedIcon: false,
                      onSelectionChanged: (values) =>
                          setState(() => _trackedOnly = values.first),
                    ),
                    const SizedBox(height: 12),
                    SingleChildScrollView(
                      scrollDirection: Axis.horizontal,
                      child: Row(
                        children: [
                          _WindowChip(
                            label: 'Next 7 days',
                            selected: _windowDays == 7,
                            onTap: () => setState(() => _windowDays = 7),
                          ),
                          _WindowChip(
                            label: '30 days',
                            selected: _windowDays == 30,
                            onTap: () => setState(() => _windowDays = 30),
                          ),
                          _WindowChip(
                            label: '90 days',
                            selected: _windowDays == 90,
                            onTap: () => setState(() => _windowDays = 90),
                          ),
                          _WindowChip(
                            label: '6 months',
                            selected: _windowDays == 180,
                            onTap: () => setState(() => _windowDays = 180),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 14),
                    SurfaceCard(
                      padding: const EdgeInsets.all(14),
                      borderColor:
                          ByteSqueezeColors.cyan.withValues(alpha: .28),
                      child: const Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Icon(
                            Icons.radar_rounded,
                            color: ByteSqueezeColors.mint,
                          ),
                          SizedBox(width: 12),
                          Expanded(
                            child: Text(
                              'Release dates are a heads-up, not a queue promise. ByteSqueeze waits for the real recording to appear and become stable before automation can use it.',
                              style: TextStyle(fontSize: 12.5),
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 22),
                  ],
                ),
              ),
            ),
            if (days.isEmpty)
              SliverToBoxAdapter(
                child: PageInsets(
                  child: EmptyState(
                    icon: _trackedOnly
                        ? Icons.notifications_off_outlined
                        : Icons.event_available_outlined,
                    title: _trackedOnly
                        ? 'No tracked releases in this window'
                        : 'Your release radar is clear',
                    message: _trackedOnly
                        ? 'Track a show in Library or choose a wider date range.'
                        : 'Choose a wider date range, track a show, or refresh the Library to fetch new release dates.',
                    action: OutlinedButton.icon(
                      onPressed: () => setState(() {
                        _trackedOnly = false;
                        _windowDays = 180;
                      }),
                      icon: const Icon(Icons.zoom_out_map_rounded),
                      label: const Text('Show the next 6 months'),
                    ),
                  ),
                ),
              )
            else
              SliverList.builder(
                itemCount: days.length,
                itemBuilder: (context, index) {
                  final day = days[index];
                  return PageInsets(
                    child: _ReleaseDay(
                      date: '${day['date'] ?? ''}',
                      episodes: asList(day['episodes']).map(asMap).toList(),
                    ),
                  );
                },
              ),
            const SliverToBoxAdapter(
              child: Padding(
                padding: EdgeInsets.fromLTRB(24, 8, 24, 118),
                child: Text(
                  'Schedule data by TVmaze · CC BY-SA',
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    color: ByteSqueezeColors.muted,
                    fontSize: 11.5,
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _RadarHero extends StatelessWidget {
  const _RadarHero({
    required this.episodeCount,
    required this.trackedCount,
    required this.showCount,
    required this.nextDate,
    required this.today,
  });

  final int episodeCount;
  final int trackedCount;
  final int showCount;
  final DateTime? nextDate;
  final DateTime today;

  @override
  Widget build(BuildContext context) {
    final nextLabel = nextDate == null
        ? 'No date'
        : _relativeDate(nextDate!, today, short: true);
    return DecoratedBox(
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFF122936), Color(0xFF111827), Color(0xFF171329)],
        ),
        borderRadius: BorderRadius.circular(20),
        border:
            Border.all(color: ByteSqueezeColors.cyan.withValues(alpha: .28)),
      ),
      child: Padding(
        padding: const EdgeInsets.all(17),
        child: LayoutBuilder(
          builder: (context, constraints) {
            final cards = [
              _RadarMetric(
                icon: Icons.play_circle_outline_rounded,
                value: '$episodeCount',
                label: 'episodes',
                color: ByteSqueezeColors.cyan,
              ),
              _RadarMetric(
                icon: Icons.live_tv_rounded,
                value: '$showCount',
                label: 'shows',
                color: ByteSqueezeColors.violet,
              ),
              _RadarMetric(
                icon: Icons.notifications_active_outlined,
                value: '$trackedCount',
                label: 'tracked',
                color: ByteSqueezeColors.mint,
              ),
              _RadarMetric(
                icon: Icons.schedule_rounded,
                value: nextLabel,
                label: 'next release',
                color: ByteSqueezeColors.amber,
              ),
            ];
            if (constraints.maxWidth >= 620) {
              return Row(
                children: [
                  for (var index = 0; index < cards.length; index++) ...[
                    Expanded(child: cards[index]),
                    if (index != cards.length - 1) const SizedBox(width: 10),
                  ],
                ],
              );
            }
            return Wrap(
              spacing: 10,
              runSpacing: 10,
              children: cards
                  .map((card) => SizedBox(
                        width: (constraints.maxWidth - 10) / 2,
                        child: card,
                      ))
                  .toList(),
            );
          },
        ),
      ),
    );
  }
}

class _RadarMetric extends StatelessWidget {
  const _RadarMetric({
    required this.icon,
    required this.value,
    required this.label,
    required this.color,
  });

  final IconData icon;
  final String value;
  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) => DecoratedBox(
        decoration: BoxDecoration(
          color: Colors.black.withValues(alpha: .16),
          borderRadius: BorderRadius.circular(14),
          border: Border.all(color: color.withValues(alpha: .18)),
        ),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(icon, color: color, size: 19),
              const SizedBox(height: 9),
              Text(
                value,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style:
                    const TextStyle(fontWeight: FontWeight.w900, fontSize: 17),
              ),
              Text(
                label,
                style: const TextStyle(
                  color: ByteSqueezeColors.muted,
                  fontSize: 11,
                ),
              ),
            ],
          ),
        ),
      );
}

class _WindowChip extends StatelessWidget {
  const _WindowChip({
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(right: 8),
        child: ChoiceChip(
          label: Text(label),
          selected: selected,
          onSelected: (_) => onTap(),
          avatar: selected ? const Icon(Icons.check_rounded, size: 16) : null,
        ),
      );
}

class _ReleaseDay extends StatelessWidget {
  const _ReleaseDay({required this.date, required this.episodes});

  final String date;
  final List<Map<String, dynamic>> episodes;

  @override
  Widget build(BuildContext context) {
    final parsed = DateTime.tryParse(date);
    final now = DateTime.now();
    final today = DateTime(now.year, now.month, now.day);
    final accent = parsed != null && parsed.difference(today).inDays <= 1
        ? ByteSqueezeColors.cyan
        : ByteSqueezeColors.softInk;
    return Padding(
      padding: const EdgeInsets.only(bottom: 22),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              DecoratedBox(
                decoration: BoxDecoration(
                  color: accent.withValues(alpha: .1),
                  borderRadius: BorderRadius.circular(999),
                  border: Border.all(color: accent.withValues(alpha: .25)),
                ),
                child: Padding(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 11, vertical: 7),
                  child: Text(
                    parsed == null ? date : _relativeDate(parsed, today),
                    style: TextStyle(
                      color: accent,
                      fontSize: 12,
                      fontWeight: FontWeight.w800,
                      letterSpacing: .35,
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 10),
              Flexible(
                child: Text(
                  parsed == null ? '' : _dateLabel(parsed),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: ByteSqueezeColors.muted,
                    fontSize: 12,
                  ),
                ),
              ),
              const Expanded(child: Divider(indent: 10)),
              const SizedBox(width: 9),
              StatusPill(
                label:
                    '${episodes.length} ${episodes.length == 1 ? 'episode' : 'episodes'}',
                color: accent,
              ),
            ],
          ),
          const SizedBox(height: 11),
          ...episodes.map(_EpisodeCard.new),
        ],
      ),
    );
  }
}

class _EpisodeCard extends StatelessWidget {
  const _EpisodeCard(this.episode);

  final Map<String, dynamic> episode;

  @override
  Widget build(BuildContext context) {
    final season = (episode['season'] as num?)?.toInt() ?? 0;
    final number = (episode['episode'] as num?)?.toInt() ?? 0;
    final tracked = episode['tracked'] == true;
    final airtime = '${episode['airtime'] ?? ''}'.trim();
    final art = {
      'title': episode['show_title'],
      'poster_url': episode['poster_url'],
    };
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: SurfaceCard(
        padding: const EdgeInsets.all(12),
        borderColor: tracked
            ? ByteSqueezeColors.mint.withValues(alpha: .25)
            : ByteSqueezeColors.subtleLine,
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 66,
              height: 92,
              child: PosterArt(item: art, borderRadius: 12),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          '${episode['show_title'] ?? 'Unknown show'}',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            fontWeight: FontWeight.w900,
                            fontSize: 15,
                          ),
                        ),
                      ),
                      if (tracked)
                        const StatusPill(
                          label: 'Tracked',
                          icon: Icons.notifications_active_rounded,
                          color: ByteSqueezeColors.mint,
                        ),
                    ],
                  ),
                  const SizedBox(height: 7),
                  Text(
                    '${episode['name'] ?? 'New episode'}',
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: ByteSqueezeColors.softInk,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const SizedBox(height: 9),
                  Wrap(
                    spacing: 7,
                    runSpacing: 7,
                    children: [
                      MediaOperationTag(
                        label:
                            'S${season.toString().padLeft(2, '0')}E${number.toString().padLeft(2, '0')}',
                        icon: Icons.live_tv_rounded,
                        color: ByteSqueezeColors.cyan,
                      ),
                      if (airtime.isNotEmpty)
                        MediaOperationTag(
                          label: airtime,
                          icon: Icons.schedule_rounded,
                          color: ByteSqueezeColors.amber,
                        ),
                    ],
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

String _relativeDate(DateTime date, DateTime today, {bool short = false}) {
  final clean = DateTime(date.year, date.month, date.day);
  final days = clean.difference(today).inDays;
  if (days == 0) return 'Today';
  if (days == 1) return 'Tomorrow';
  if (days < 7) return short ? '${days}d' : 'In $days days';
  if (short) return '${days}d';
  return 'In ${(days / 7).ceil()} weeks';
}

String _dateLabel(DateTime date) {
  const months = [
    'Jan',
    'Feb',
    'Mar',
    'Apr',
    'May',
    'Jun',
    'Jul',
    'Aug',
    'Sep',
    'Oct',
    'Nov',
    'Dec',
  ];
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  return '${days[date.weekday - 1]}, ${months[date.month - 1]} ${date.day}';
}
