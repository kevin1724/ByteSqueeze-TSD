import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'src/app_controller.dart';
import 'src/app_meta.dart';
import 'src/screens/app_shell.dart';
import 'src/screens/pairing_screen.dart';
import 'src/theme.dart';

const _pairingLinkChannel = MethodChannel('com.kevina1724.bytesqueeze/links');

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  final controller = AppController();
  _pairingLinkChannel.setMethodCallHandler((call) async {
    if (call.method == 'pairingLink' && call.arguments is String) {
      await controller.handleQuickPairingLink(call.arguments as String);
    }
  });
  runApp(ByteSqueezeApp(controller: controller));
  unawaited(_initializeApp(controller));
}

Future<void> _initializeApp(AppController controller) async {
  String initialLink = '';
  try {
    initialLink =
        await _pairingLinkChannel.invokeMethod<String>('getInitialLink') ?? '';
  } on MissingPluginException {
    // The shared iOS/desktop build can continue without Android link delivery.
  } on PlatformException {
    // Manual pairing remains available if Android cannot provide the intent.
  }
  await controller.bootstrap();
  if (initialLink.isNotEmpty) {
    await controller.handleQuickPairingLink(initialLink);
  }
}

class ByteSqueezeApp extends StatelessWidget {
  const ByteSqueezeApp({super.key, required this.controller});

  final AppController controller;

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: controller,
      builder: (context, _) {
        return MaterialApp(
          title: 'ByteSqueeze',
          debugShowCheckedModeBanner: false,
          theme: controller.useV3
              ? ByteSqueezeTheme.v3(compact: controller.compactInterface)
              : ByteSqueezeTheme.classic,
          themeAnimationDuration: const Duration(milliseconds: 280),
          home: AnimatedSwitcher(
            duration: const Duration(milliseconds: 360),
            child: controller.booting
                ? const _LaunchScreen(key: ValueKey('launch'))
                : controller.connected
                    ? AppShell(
                        key: const ValueKey('shell'), controller: controller)
                    : PairingScreen(
                        key: const ValueKey('pair'), controller: controller),
          ),
        );
      },
    );
  }
}

class _LaunchScreen extends StatelessWidget {
  const _LaunchScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: DecoratedBox(
        decoration: const BoxDecoration(gradient: ByteSqueezeColors.backdrop),
        child: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ClipRRect(
                borderRadius: BorderRadius.circular(36),
                child: Image.asset(
                  'assets/branding/bytesqueeze_icon.png',
                  width: 132,
                  height: 132,
                ),
              ),
              const SizedBox(height: 22),
              Text('ByteSqueeze',
                  style: Theme.of(context).textTheme.headlineMedium),
              const SizedBox(height: 6),
              const Text('$appReleaseLabel · $appVersion',
                  style: TextStyle(
                      color: ByteSqueezeColors.muted,
                      fontSize: 11,
                      fontWeight: FontWeight.w700,
                      letterSpacing: .8)),
              const SizedBox(height: 18),
              const SizedBox(
                width: 28,
                height: 28,
                child: CircularProgressIndicator(strokeWidth: 2.5),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
