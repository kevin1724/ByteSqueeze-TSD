class QuickPairingRequest {
  const QuickPairingRequest({required this.server, required this.code});

  final String server;
  final String code;

  static QuickPairingRequest? tryParse(String value) {
    final uri = Uri.tryParse(value.trim());
    if (uri == null || uri.scheme.toLowerCase() != 'bytesqueeze') return null;
    final route = uri.host.isNotEmpty ? uri.host : uri.path.replaceAll('/', '');
    if (route.toLowerCase() != 'pair') return null;
    final server = (uri.queryParameters['server'] ?? '').trim();
    final code = (uri.queryParameters['code'] ?? '').trim().toUpperCase();
    final serverUri = Uri.tryParse(server);
    if (serverUri == null ||
        !{'http', 'https'}.contains(serverUri.scheme.toLowerCase()) ||
        serverUri.host.isEmpty) {
      return null;
    }
    if (code.replaceAll('-', '').length != 8 ||
        !RegExp(r'^[A-Z0-9-]+$').hasMatch(code)) {
      return null;
    }
    return QuickPairingRequest(
      server: server.replaceAll(RegExp(r'/+$'), ''),
      code: code,
    );
  }
}
