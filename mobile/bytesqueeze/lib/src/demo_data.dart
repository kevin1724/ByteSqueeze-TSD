abstract final class DemoData {
  static Map<String, dynamic> get dashboard => {
    'release': '3.17.0',
    'queue': {
      'paused': false,
      'summary': {
        'queued': 3,
        'running': 1,
        'done': 148,
        'error': 0,
        'hardware_transcode_concurrency': 2,
        'saved_bytes': 1897366597632,
      },
    },
    'active_jobs': jobs['jobs'],
    'library': {
      'movies': 684,
      'shows': 92,
      'configured': true,
      'last_scan_at': 1785126840,
    },
    'nodes': {
      'paired': 3,
      'online': 3,
      'local': {'name': 'TSD Main', 'online': true, 'status': 'running'},
    },
    'automation': automation,
    'storage': {
      'count': 148,
      'saved_bytes': 1897366597632,
      'saved_gb': 1767.3,
      'analytics': storageAnalytics,
    },
    'events': events['events'],
  };

  static Map<String, dynamic> get jobs => {
    'paused': false,
    'summary': {
      'queued': 3,
      'running': 1,
      'done': 148,
      'error': 0,
      'hardware_transcode_concurrency': 2,
    },
    'jobs': [
      {
        'id': 'demo-running',
        'src': '/movies/Arrival (2016)/Arrival.2160p.Remux.mkv',
        'preset': 'smart',
        'status': 'running',
        'progress': 67.4,
        'eta_seconds': 1248,
        'encoder': 'qsv_h265_10bit',
        'node_name': 'TSD Main',
        'estimated_out_gb': 8.4,
      },
      {
        'id': 'demo-queued-1',
        'src': '/shows/Foundation/Season 03/Foundation.S03E04.mkv',
        'preset': 'smart',
        'status': 'queued',
        'progress': 0,
        'encoder': 'x265_10bit',
        'queued_preset_name': 'Smart HEVC 10-bit',
        'node_id': 'node-2',
        'node_name': 'Office Encoder',
        'is_worker_job': true,
      },
      {
        'id': 'demo-queued-2',
        'src': '/movies/Dune Part Two (2024)/Dune.Part.Two.2160p.mkv',
        'preset': '4k',
        'status': 'queued',
        'progress': 0,
        'encoder': 'qsv_h265_10bit',
      },
      {
        'id': 'demo-done',
        'src': '/movies/Blade Runner 2049/Blade.Runner.2049.2160p.mkv',
        'preset': 'smart',
        'status': 'done',
        'progress': 100,
        'saved_bytes': 18253611008,
        'encoder': 'x265_10bit',
      },
    ],
  };

  static Map<String, dynamic> get library => {
    'configured': true,
    'stats': {'movies': 8, 'shows': 5, 'episodes': 164, 'scanned': 172},
    'catalog': {
      'complete': true,
      'total_titles': 13,
      'recently_added': [
        _movie('Dune: Part Two', 2024, 60245199872, '4K HDR', 78),
        _movie('Oppenheimer', 2023, 71403831296, '4K HDR', 81),
        _movie('Arrival', 2016, 31890132112, '4K HDR', 61),
      ],
    },
    'movies': [
      _movie('Arrival', 2016, 31890132112, '4K HDR', 61),
      _movie('Blade Runner 2049', 2017, 52122144768, '4K HDR', 72),
      _movie('Dune: Part Two', 2024, 60245199872, '4K HDR', 78),
      _movie(
        'Everything Everywhere All at Once',
        2022,
        25419616256,
        '1080p',
        35,
      ),
      _movie('Interstellar', 2014, 48726679552, '4K HDR', 68),
      _movie('Mad Max: Fury Road', 2015, 36410818560, '4K HDR', 58),
      _movie('Oppenheimer', 2023, 71403831296, '4K HDR', 81),
      _movie(
        'Spider-Man: Across the Spider-Verse',
        2023,
        28991029248,
        '4K',
        43,
      ),
    ],
    'shows': [
      _show('Andor', 2022, 2, 24, false, 13),
      _show('Foundation', 2021, 3, 30, true, 24),
      _show('Severance', 2022, 2, 19, true, 34),
      _show('Silo', 2023, 2, 20, false, 45),
      _show('The Last of Us', 2023, 2, 16, true, 56),
    ],
  };

  static Map<String, dynamic> get calendar => {
    'provider': {'name': 'TVmaze', 'url': 'https://www.tvmaze.com/'},
    'count': 4,
    'days': [
      {
        'date': '2026-09-23',
        'episodes': [
          {
            'show_title': 'Foundation',
            'season': 3,
            'episode': 5,
            'name': 'The Weight of Worlds',
            'airdate': '2026-09-23',
            'airtime': '9:00 PM',
            'poster_url': '',
            'tracked': true,
          },
        ],
      },
      {
        'date': '2026-09-27',
        'episodes': [
          {
            'show_title': 'Severance',
            'season': 3,
            'episode': 1,
            'name': 'After Hours',
            'airdate': '2026-09-27',
            'airtime': '8:00 PM',
            'poster_url': '',
            'tracked': true,
          },
          {
            'show_title': 'Silo',
            'season': 3,
            'episode': 2,
            'name': 'The Descent',
            'airdate': '2026-09-27',
            'airtime': '10:00 PM',
            'poster_url': '',
            'tracked': false,
          },
        ],
      },
    ],
  };

  static Map<String, dynamic> get automation => {
    'settings': {
      'autopilot_enabled': true,
      'autopilot_mode': 'observe',
      'autopilot_include_movies': true,
      'autopilot_include_shows': true,
      'autopilot_min_size_gb': 2.0,
      'autopilot_min_savings_percent': 12.0,
      'autopilot_batch_limit': 3,
      'autopilot_max_active_jobs': 5,
      'autopilot_schedule_start': '01:00',
      'autopilot_schedule_end': '07:00',
      'beta_auto_scan_enabled': true,
      'beta_auto_scan_interval_minutes': 30,
    },
    'status': {
      'autopilot': {
        'enabled': true,
        'mode': 'observe',
        'eligible': 18,
        'selected': 3,
        'queued': 0,
        'schedule_open': false,
        'active_jobs': 4,
        'capacity': 1,
        'decisions': [
          {
            'decision': 'eligible',
            'title': 'Arrival',
            'reason': 'Estimated 61% storage savings with a Smart Preset.',
          },
          {
            'decision': 'wait',
            'title': 'Foundation S03E04',
            'reason': 'Waiting for the file-stability window.',
          },
          {
            'decision': 'skip',
            'title': 'Interstellar',
            'reason': 'An active queue job already owns this file.',
          },
        ],
      },
      'readiness': {
        'ready': true,
        'checks': [
          {
            'ok': true,
            'label': 'Library folders',
            'detail': '2 accessible media folders.',
          },
          {
            'ok': true,
            'label': 'Durable data',
            'detail': 'Automation storage is writable.',
          },
          {
            'ok': true,
            'label': 'Write protection',
            'detail': 'New files must become stable before queueing.',
          },
          {
            'ok': true,
            'label': 'Smart Presets',
            'detail': 'Learned selection is ready.',
          },
        ],
      },
    },
  };

  static Map<String, dynamic> get nodes => {
    'local': {
      'id': 'local',
      'name': 'TSD Main',
      'online': true,
      'status': 'running',
      'protocol_version': 2,
    },
    'nodes': [
      {
        'id': 'node-1',
        'name': 'Living Room Mini PC',
        'url': 'http://192.168.12.110:8082',
        'online': true,
        'status': 'idle',
        'hardware_transcode_concurrency': 2,
        'summary': {
          'counts': {'running': 0, 'queued': 0, 'error': 0},
        },
        'protocol_version': 2,
      },
      {
        'id': 'node-2',
        'name': 'Office Encoder',
        'url': 'http://192.168.12.111:8082',
        'online': true,
        'status': 'queued',
        'hardware_transcode_concurrency': 3,
        'summary': {
          'counts': {'running': 0, 'queued': 1, 'error': 0},
        },
        'protocol_version': 2,
      },
    ],
  };

  static Map<String, dynamic> get storage => {
    'summary': {
      'count': 148,
      'saved_bytes': 1897366597632,
      'saved_gb': 1767.3,
      'total_runtime_seconds': 762884,
    },
    'analytics': storageAnalytics,
    'encodes': [
      {
        'src': '/movies/Blade Runner 2049.mkv',
        'saved_bytes': 18253611008,
        'preset': 'smart',
        'encoder': 'x265_10bit',
        'ts': 1785124400,
      },
      {
        'src': '/shows/Severance.S02E10.mkv',
        'saved_bytes': 3865470566,
        'preset': '1080',
        'encoder': 'qsv_h265_10bit',
        'ts': 1785038000,
      },
    ],
  };

  static Map<String, dynamic> get storageAnalytics => {
    'days': 30,
    'recent': {
      'saved_bytes': 238370684928,
      'completed': 24,
      'active_days': 13,
      'longest_streak': 6,
      'peak_saved_bytes': 37580963840,
      'peak_date': '2026-08-23',
    },
    'efficiency_percent': 66.8,
    'average_saved_bytes': 12820044578,
    'trend': [
      {'date': '2026-08-14', 'saved_bytes': 5368709120, 'completed': 1},
      {'date': '2026-08-15', 'saved_bytes': 11811160064, 'completed': 2},
      {'date': '2026-08-16', 'saved_bytes': 8589934592, 'completed': 1},
      {'date': '2026-08-17', 'saved_bytes': 17179869184, 'completed': 2},
      {'date': '2026-08-18', 'saved_bytes': 13958643712, 'completed': 2},
      {'date': '2026-08-19', 'saved_bytes': 21474836480, 'completed': 2},
      {'date': '2026-08-20', 'saved_bytes': 18790481920, 'completed': 2},
      {'date': '2026-08-21', 'saved_bytes': 26843545600, 'completed': 3},
      {'date': '2026-08-22', 'saved_bytes': 23622320128, 'completed': 2},
      {'date': '2026-08-23', 'saved_bytes': 37580963840, 'completed': 4},
      {'date': '2026-08-24', 'saved_bytes': 27917287424, 'completed': 2},
      {'date': '2026-08-25', 'saved_bytes': 32212254720, 'completed': 2},
      {'date': '2026-08-26', 'saved_bytes': 24696061952, 'completed': 1},
      {'date': '2026-08-27', 'saved_bytes': 28991029248, 'completed': 2},
    ],
    'workers': [
      {
        'node_id': 'local',
        'node_name': 'TSD Main',
        'completed': 72,
        'saved_bytes': 934155386880,
        'runtime_seconds': 368410,
        'share_percent': 49.2,
      },
      {
        'node_id': 'node-2',
        'node_name': 'Office Encoder',
        'completed': 46,
        'saved_bytes': 606664130560,
        'runtime_seconds': 248400,
        'share_percent': 32.0,
      },
      {
        'node_id': 'node-1',
        'node_name': 'Living Room Mini PC',
        'completed': 30,
        'saved_bytes': 356547080192,
        'runtime_seconds': 146074,
        'share_percent': 18.8,
      },
    ],
  };

  static Map<String, dynamic> get events => {
    'events': [
      {
        'level': 'info',
        'type': 'job_progress',
        'message': 'Arrival reached 67% with 21 minutes remaining.',
        'ts': 1785126840,
      },
      {
        'level': 'info',
        'type': 'autopilot_scan',
        'message': 'Autopilot found 18 eligible media files.',
        'ts': 1785123240,
      },
      {
        'level': 'warn',
        'type': 'node_recovered',
        'message': 'Office Encoder recovered its paired session.',
        'ts': 1785116040,
      },
      {
        'level': 'info',
        'type': 'job_done',
        'message': 'Blade Runner 2049 saved 17.0 GB.',
        'ts': 1785036840,
      },
    ],
  };

  static Map<String, dynamic> get smart => {
    'profile': {
      'goal': 'balanced',
      'compatibility': 'modern',
      'hardware': 'auto',
      'audio_strategy': 'copy',
      'audio_languages': ['eng', 'spa'],
      'subtitle_languages': ['eng', 'spa'],
      'never_downscale': true,
      'keep_black_bars': true,
      'keep_aspect_ratio': true,
      'never_transcode_audio': true,
      'keep_all_audio_languages': true,
      'keep_all_subtitle_languages': true,
      'automation_enabled': true,
    },
    'learning': {
      'automation_ready': true,
      'feedback_count': 12,
      'approval_probability': .84,
      'message': 'Ready to choose similar presets automatically.',
    },
  };

  static Map<String, dynamic> get operations => {
    'hardware_transcode_concurrency': 2,
    'qsv_device_available': true,
    'auto_stop_large_output_enabled': true,
    'auto_stop_large_output_percent': 90.0,
    'audio_policy_default': 'preserve',
    'audio_optimize_codec': 'aac',
    'audio_optimize_bitrate_kbps': 1024,
    'audio_deduplicate_languages': true,
    'audio_preferred_languages': ['eng', 'spa'],
  };

  static Map<String, dynamic> _movie(
    String title,
    int year,
    int bytes,
    String quality,
    int hue,
  ) => {
    'id': 'movie-${title.hashCode}',
    'type': 'movie',
    'title': title,
    'year': year,
    'size_bytes': bytes,
    'quality': quality,
    'demo_hue': hue,
    'path': '/movies/$title ($year)/$title.mkv',
    'paths': ['/movies/$title ($year)/$title.mkv'],
    'poster_url': '',
    'prediction': {'available': true, 'savings_percent': hue},
  };

  static Map<String, dynamic> _show(
    String title,
    int year,
    int seasons,
    int episodes,
    bool tracked,
    int hue,
  ) => {
    'id': 'show-${title.hashCode}',
    'type': 'show',
    'title': title,
    'year': year,
    'season_count': seasons,
    'episode_count': episodes,
    'tracked': tracked,
    'demo_hue': hue,
    'poster_url': '',
    'files': List.generate(
      episodes.clamp(1, 4).toInt(),
      (index) => {
        'title': '$title episode ${index + 1}',
        'season': 1,
        'episode': index + 1,
        'path': '/shows/$title/Season 01/$title.S01E0${index + 1}.mkv',
      },
    ),
  };
}
