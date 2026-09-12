import 'package:bytesqueeze/src/api_client.dart';
import 'package:bytesqueeze/src/app_controller.dart';
import 'package:bytesqueeze/src/session_store.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('Size Wizard automatic estimates react to media choices', () async {
    final controller = AppController()..enterDemo();

    final balanced = await controller.planSizeWizard('/demo/Arrival.mkv');
    final smaller = await controller.planSizeWizard(
      '/demo/Arrival.mkv',
      options: {
        'target_size_auto': true,
        'quality': 'small',
        'video_codec': 'av1',
        'resolution_mode': '720',
      },
    );

    final balancedMb =
        ((balanced['plan'] as Map)['inputs'] as Map)['target_mb'] as num;
    final smallerMb =
        ((smaller['plan'] as Map)['inputs'] as Map)['target_mb'] as num;
    expect(balancedMb, isNot(5120));
    expect(smallerMb, lessThan(balancedMb));
    expect(
      (((smaller['plan'] as Map)['estimates'] as Map)['auto_target']
          as Map)['mode'],
      'source_aware',
    );
  });

  test('older servers do not trigger a global connection failure', () async {
    final controller = AppController();
    controller.api = _LegacyServerApi(controller.store);

    await controller.refreshAll(notifyBusy: false);

    expect(controller.error, isNull);
    expect(controller.serverSupportsOperationsSettings, isFalse);
    expect(controller.operations['hardware_transcode_concurrency'], 3);
    expect(controller.dashboard['ok'], isTrue);
  });

  test('library queue forwards next available as a distributed destination',
      () async {
    final controller = AppController();
    final api = _RecordingApi(controller.store);
    controller.api = api;
    controller.session = const ServerSession(
      baseUrl: 'http://bytesqueeze.test',
      deviceId: 'distribution-phone',
      deviceName: 'Distribution phone',
      scope: 'control',
      accessToken: 'access',
      refreshToken: 'refresh',
    );

    await controller.queuePaths(
      const ['/shows/Example.S01E01.mkv', '/shows/Example.S01E02.mkv'],
      preset: 'smart',
      mode: 'available',
    );

    expect(api.lastPostPath, '/library/queue');
    expect(api.lastPostBody['mode'], 'available');
    expect(api.lastPostBody['paths'], hasLength(2));
    expect(api.lastPostBody.containsKey('node_id'), isFalse);
  });

  test('audio-only queue preserves the stream plan and uses next available',
      () async {
    final controller = AppController();
    final api = _RecordingApi(controller.store);
    controller.api = api;
    controller.session = const ServerSession(
      baseUrl: 'http://bytesqueeze.test',
      deviceId: 'audio-phone',
      deviceName: 'Audio phone',
      scope: 'control',
      accessToken: 'access',
      refreshToken: 'refresh',
    );
    final operations = <String, dynamic>{
      'job_type': 'audio_only',
      'video_action': 'copy',
      'audio_policy': 'custom',
      'audio_actions': [
        {
          'stream_index': 1,
          'action': 'encode',
          'target_codec': 'aac',
          'bitrate_kbps': 640,
          'channels': 6,
        }
      ],
    };

    await controller.queueAudioOptimizationPath(
      '/movies/Example-TSD.mkv',
      operations,
    );

    expect(api.lastPostPath, '/audio/queue');
    expect(api.lastPostBody['mode'], 'next_available');
    expect(api.lastPostBody['src'], '/movies/Example-TSD.mkv');
    final sentOperations =
        Map<String, dynamic>.from(api.lastPostBody['operations'] as Map);
    expect(sentOperations['video_action'], 'copy');
    expect(sentOperations['audio_actions'], hasLength(1));
  });
}

class _RecordingApi extends ByteSqueezeApi {
  _RecordingApi(super.store);

  String lastPostPath = '';
  Map<String, dynamic> lastPostBody = <String, dynamic>{};

  @override
  Future<Map<String, dynamic>> post(
    String path,
    Map<String, dynamic>? body, {
    Duration? timeout,
  }) async {
    lastPostPath = path;
    lastPostBody = Map<String, dynamic>.from(body ?? const {});
    return {'ok': true};
  }

  @override
  Future<Map<String, dynamic>> get(String path, {Duration? timeout}) async {
    return {'ok': true};
  }
}

class _LegacyServerApi extends ByteSqueezeApi {
  _LegacyServerApi(super.store);

  @override
  Future<Map<String, dynamic>> get(String path, {Duration? timeout}) async {
    if (path == '/operations') {
      throw const ApiFailure(
        'This app feature is not available on the connected server yet.',
        statusCode: 404,
      );
    }
    if (path == '/jobs') {
      return {
        'ok': true,
        'summary': {'hardware_transcode_concurrency': 3},
      };
    }
    if (path == '/library') return {'ok': true, 'library': {}};
    if (path.startsWith('/calendar')) return {'ok': true, 'calendar': {}};
    if (path == '/autopilot/review') return {'ok': true, 'review': {}};
    return {'ok': true};
  }
}
