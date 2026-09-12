import 'package:bytesqueeze/src/screens/size_wizard_screen.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('manual audio choice clears a stale Smart passthrough lock', () {
    final updated = applySizeWizardOption(
      <String, dynamic>{
        'audio_mode': 'copy',
        'smart_never_transcode_audio': true,
        'ai_copy_audio': true,
        'smart_audio_strategy': 'copy',
      },
      'audio_mode',
      'eac3',
    );

    expect(updated['audio_mode'], 'eac3');
    expect(updated['smart_never_transcode_audio'], isFalse);
    expect(updated['ai_copy_audio'], isFalse);
    expect(updated['smart_audio_strategy'], 'eac3_surround');
  });

  test('switching back to copy also clears the previous audio strategy', () {
    final updated = applySizeWizardOption(
      <String, dynamic>{
        'audio_mode': 'eac3',
        'smart_never_transcode_audio': false,
        'ai_copy_audio': false,
        'smart_audio_strategy': 'eac3_surround',
      },
      'audio_mode',
      'copy',
    );

    expect(updated['audio_mode'], 'copy');
    expect(updated['ai_copy_audio'], isTrue);
    expect(updated['smart_audio_strategy'], 'copy');
  });
}
