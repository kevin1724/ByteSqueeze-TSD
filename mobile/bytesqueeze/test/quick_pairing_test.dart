import 'package:bytesqueeze/src/quick_pairing.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('quick pairing link accepts an encoded controller URL and code', () {
    final request = QuickPairingRequest.tryParse(
      'bytesqueeze://pair?server=http%3A%2F%2F100.111.94.118%3A8081&code=ABCD-EFGH',
    );
    expect(request, isNotNull);
    expect(request!.server, 'http://100.111.94.118:8081');
    expect(request.code, 'ABCD-EFGH');
  });

  test('quick pairing rejects unsafe or incomplete links', () {
    expect(
      QuickPairingRequest.tryParse(
        'bytesqueeze://pair?server=file%3A%2F%2Fmedia&code=ABCD-EFGH',
      ),
      isNull,
    );
    expect(
      QuickPairingRequest.tryParse('bytesqueeze://pair?server=http://server'),
      isNull,
    );
  });
}
