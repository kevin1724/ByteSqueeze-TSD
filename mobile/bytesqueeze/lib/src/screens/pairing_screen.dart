import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app_controller.dart';
import '../theme.dart';
import '../widgets/common.dart';

class PairingScreen extends StatefulWidget {
  const PairingScreen({super.key, required this.controller});

  final AppController controller;

  @override
  State<PairingScreen> createState() => _PairingScreenState();
}

class _PairingScreenState extends State<PairingScreen> {
  final _formKey = GlobalKey<FormState>();
  final _server = TextEditingController(text: 'http://');
  final _fallbackServer = TextEditingController();
  final _code = TextEditingController();
  final _name = TextEditingController(text: 'ByteSqueeze phone');
  bool _showAdvanced = false;
  String _appliedQuickPair = '';

  void _syncQuickPairing() {
    final quickKey =
        '${widget.controller.quickPairServer}|${widget.controller.quickPairCode}';
    if (quickKey == _appliedQuickPair ||
        widget.controller.quickPairServer.isEmpty ||
        widget.controller.quickPairCode.isEmpty) {
      return;
    }
    _appliedQuickPair = quickKey;
    _server.text = widget.controller.quickPairServer;
    _code.text = widget.controller.quickPairCode;
  }

  @override
  void dispose() {
    _server.dispose();
    _fallbackServer.dispose();
    _code.dispose();
    _name.dispose();
    super.dispose();
  }

  Future<void> _pair() async {
    if (!_formKey.currentState!.validate()) return;
    try {
      await widget.controller.pair(
        baseUrl: _server.text,
        fallbackBaseUrl: _fallbackServer.text,
        code: _code.text,
        deviceName: _name.text,
      );
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(widget.controller.error ?? 'Pairing failed.')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    _syncQuickPairing();
    return Scaffold(
      body: ColoredBox(
        color: ByteSqueezeColors.canvas,
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 20),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 520),
                child: Form(
                  key: _formKey,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Align(
                        child: Hero(
                          tag: 'bytesqueeze-icon',
                          child: ClipRRect(
                            borderRadius: BorderRadius.circular(20),
                            child: Image.asset(
                              'assets/branding/bytesqueeze_icon.png',
                              width: 82,
                              height: 82,
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(height: 18),
                      Text(
                        'Connect to ByteSqueeze',
                        style: Theme.of(context).textTheme.headlineMedium,
                        textAlign: TextAlign.center,
                      ),
                      const SizedBox(height: 6),
                      const Text(
                        'Enter the server address and one-time pairing code.',
                        style: TextStyle(
                          color: ByteSqueezeColors.muted,
                          fontSize: 13,
                        ),
                        textAlign: TextAlign.center,
                      ),
                      const SizedBox(height: 20),
                      if (widget.controller.quickPairCode.isNotEmpty) ...[
                        Container(
                          padding: const EdgeInsets.all(12),
                          decoration: BoxDecoration(
                            color:
                                ByteSqueezeColors.cyan.withValues(alpha: .08),
                            borderRadius: BorderRadius.circular(12),
                            border: Border.all(
                              color:
                                  ByteSqueezeColors.cyan.withValues(alpha: .24),
                            ),
                          ),
                          child: const Row(
                            children: [
                              Icon(Icons.mobile_friendly_rounded,
                                  color: ByteSqueezeColors.cyan),
                              SizedBox(width: 10),
                              Expanded(
                                child: Text(
                                  'Quick pairing received. Connecting securely…',
                                  style: TextStyle(fontWeight: FontWeight.w700),
                                ),
                              ),
                            ],
                          ),
                        ),
                        const SizedBox(height: 12),
                      ],
                      SurfaceCard(
                        padding: const EdgeInsets.all(16),
                        borderColor: ByteSqueezeColors.cyan.withValues(
                          alpha: .3,
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            Row(
                              children: [
                                const Icon(
                                  Icons.link_rounded,
                                  color: ByteSqueezeColors.cyan,
                                ),
                                const SizedBox(width: 10),
                                Expanded(
                                  child: Text(
                                    'Server connection',
                                    style:
                                        Theme.of(context).textTheme.titleMedium,
                                  ),
                                ),
                              ],
                            ),
                            const SizedBox(height: 20),
                            TextFormField(
                              controller: _server,
                              keyboardType: TextInputType.url,
                              autocorrect: false,
                              textInputAction: TextInputAction.next,
                              decoration: const InputDecoration(
                                labelText: 'TSD server address',
                                hintText: 'http://192.168.1.50:8080',
                                prefixIcon: Icon(Icons.dns_outlined),
                              ),
                              validator: (value) =>
                                  (value ?? '').trim().length < 4
                                      ? 'Enter the server address.'
                                      : null,
                            ),
                            const SizedBox(height: 12),
                            TextFormField(
                              controller: _code,
                              textCapitalization: TextCapitalization.characters,
                              autocorrect: false,
                              inputFormatters: [
                                FilteringTextInputFormatter.allow(
                                  RegExp('[a-zA-Z0-9-]'),
                                ),
                                LengthLimitingTextInputFormatter(9),
                              ],
                              decoration: const InputDecoration(
                                labelText: 'One-time pairing code',
                                hintText: 'ABCD-EFGH',
                                prefixIcon: Icon(Icons.password_rounded),
                              ),
                              validator: (value) =>
                                  (value ?? '').replaceAll('-', '').length != 8
                                      ? 'Enter the eight-character code.'
                                      : null,
                              onFieldSubmitted: (_) => _pair(),
                            ),
                            const SizedBox(height: 8),
                            TextButton.icon(
                              onPressed: () => setState(
                                () => _showAdvanced = !_showAdvanced,
                              ),
                              icon: Icon(
                                _showAdvanced
                                    ? Icons.expand_less
                                    : Icons.tune_rounded,
                              ),
                              label: Text(
                                _showAdvanced
                                    ? 'Hide connection options'
                                    : 'Connection options',
                              ),
                            ),
                            AnimatedSize(
                              duration: const Duration(milliseconds: 220),
                              child: _showAdvanced
                                  ? Padding(
                                      padding: const EdgeInsets.only(
                                        bottom: 12,
                                      ),
                                      child: Column(
                                        children: [
                                          TextFormField(
                                            controller: _fallbackServer,
                                            keyboardType: TextInputType.url,
                                            autocorrect: false,
                                            decoration: const InputDecoration(
                                              labelText:
                                                  'Away / Tailscale address (optional)',
                                              hintText: 'http://100.x.x.x:8080',
                                              prefixIcon: Icon(
                                                Icons.route_rounded,
                                              ),
                                              helperText:
                                                  'Used automatically when the home address cannot be reached.',
                                            ),
                                          ),
                                          const SizedBox(height: 12),
                                          TextFormField(
                                            controller: _name,
                                            decoration: const InputDecoration(
                                              labelText: 'Device name',
                                              prefixIcon: Icon(
                                                Icons.phone_android_rounded,
                                              ),
                                            ),
                                          ),
                                        ],
                                      ),
                                    )
                                  : const SizedBox.shrink(),
                            ),
                            FilledButton.icon(
                              onPressed: widget.controller.busy ? null : _pair,
                              icon: widget.controller.busy
                                  ? const SizedBox(
                                      width: 18,
                                      height: 18,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2,
                                      ),
                                    )
                                  : const Icon(Icons.link_rounded),
                              label: Text(
                                widget.controller.busy ? 'Pairing…' : 'Connect',
                              ),
                            ),
                          ],
                        ),
                      ),
                      if (widget.controller.error != null) ...[
                        const SizedBox(height: 12),
                        Text(
                          widget.controller.error!,
                          textAlign: TextAlign.center,
                          style: const TextStyle(
                            color: ByteSqueezeColors.danger,
                            fontSize: 12.5,
                          ),
                        ),
                      ],
                      const SizedBox(height: 16),
                      OutlinedButton.icon(
                        onPressed: widget.controller.busy
                            ? null
                            : widget.controller.enterDemo,
                        icon: const Icon(Icons.auto_awesome_rounded),
                        label: const Text('Explore the demo dashboard'),
                        style: OutlinedButton.styleFrom(
                          padding: const EdgeInsets.symmetric(vertical: 15),
                          side: const BorderSide(color: ByteSqueezeColors.line),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(16),
                          ),
                        ),
                      ),
                      const SizedBox(height: 16),
                      const Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Icon(
                            Icons.info_outline_rounded,
                            size: 18,
                            color: ByteSqueezeColors.muted,
                          ),
                          SizedBox(width: 9),
                          Expanded(
                            child: Text(
                              'Generate a code in ByteSqueeze Settings → Linked Nodes → Companion app access.',
                              style: TextStyle(
                                color: ByteSqueezeColors.muted,
                                fontSize: 12.5,
                                height: 1.4,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
