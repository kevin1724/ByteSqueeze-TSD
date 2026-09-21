import 'package:bytesqueeze/main.dart';
import 'package:bytesqueeze/src/app_controller.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
  });

  testWidgets('demo opens the ByteSqueeze dashboard', (tester) async {
    final controller = AppController()..enterDemo();
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    expect(find.text('Overview'), findsWidgets);
    expect(find.text('Encoding now'), findsOneWidget);
    expect(find.text('Storage impact'), findsOneWidget);
    expect(find.text('Saved by worker'), findsOneWidget);
    expect(find.text('Automate'), findsOneWidget);
    expect(find.text('More'), findsOneWidget);
  });

  testWidgets('iPad layout keeps every app workspace available',
      (tester) async {
    tester.view.physicalSize = const Size(1180, 820);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });

    final controller = AppController()..enterDemo();
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    expect(find.text('Overview'), findsWidgets);
    expect(find.text('Library'), findsWidgets);
    expect(find.text('Queue'), findsWidgets);
    expect(find.text('Autopilot'), findsWidgets);
    expect(find.text('Settings'), findsWidgets);
    expect(find.byType(NavigationBar), findsNothing);

    await tester.tap(find.text('Library').first);
    await tester.pumpAndSettle();
    expect(controller.selectedTab, 1);
    expect(find.textContaining('movies ·'), findsWidgets);
  });

  testWidgets('V3 command center exposes navigation and safe actions',
      (tester) async {
    final controller = AppController()..enterDemo();
    controller.showSecondaryUi = true;
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    await tester.tap(find.byTooltip('Command center'));
    await tester.pumpAndSettle();

    expect(find.text('Command center'), findsOneWidget);
    expect(find.text('Open Library'), findsOneWidget);
    await tester.scrollUntilVisible(find.text('Queue with Smart Presets'), 220,
        scrollable: find.byType(Scrollable).last);
    await tester.pumpAndSettle();
    expect(find.text('Queue with Smart Presets'), findsOneWidget);
  });

  testWidgets('settings keeps the V2 Classic fallback available',
      (tester) async {
    final controller = AppController()..enterDemo();
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    await tester.tap(find.byIcon(Icons.tune_outlined));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('Interface'), 220,
        scrollable: find.byType(Scrollable).first);
    final interfaceTile = find.widgetWithText(ListTile, 'Interface');
    await tester.drag(find.byType(ListView).first, const Offset(0, -140));
    await tester.pumpAndSettle();
    await tester.tap(interfaceTile);
    await tester.pumpAndSettle();

    expect(find.text('V3'), findsOneWidget);
    expect(find.text('V2 Classic'), findsOneWidget);
    await tester.tap(find.text('V2 Classic'));
    await tester.pumpAndSettle();
    expect(controller.useV3, isFalse);
    expect(await controller.store.loadInterfaceVersion(), 'v2');
  });

  testWidgets('release radar and creator credits are easy to find',
      (tester) async {
    tester.view.physicalSize = const Size(430, 932);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    final controller = AppController()..enterDemo();
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    await tester.tap(find.byIcon(Icons.tune_outlined));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('Upcoming episodes'), 220,
        scrollable: find.byType(Scrollable).first);
    await tester.tap(find.text('Upcoming episodes'));
    await tester.pumpAndSettle();

    expect(find.text('What’s coming next'), findsOneWidget);
    expect(find.text('All releases'), findsOneWidget);
    expect(find.text('Next 7 days'), findsOneWidget);
    expect(find.text('Foundation'), findsWidgets);

    await tester.tap(find.byType(BackButton));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
        find.text('Creator, source & licenses'), 260,
        scrollable: find.byType(Scrollable).first);
    await tester.tap(find.text('Creator, source & licenses'));
    await tester.pumpAndSettle();

    expect(find.text('Kevin Ruiz Arzate'), findsOneWidget);
    expect(find.text('Open Kevin’s GitHub'), findsOneWidget);
    expect(find.text('View ByteSqueeze source'), findsOneWidget);
    expect(find.text('Open-source licenses'), findsOneWidget);
  });

  testWidgets('mobile settings expose GPU slots and CPU exclusivity',
      (tester) async {
    final controller = AppController()..enterDemo();
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    await tester.tap(find.byIcon(Icons.tune_outlined));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
        find.text('Encoding capacity & safety'), 220,
        scrollable: find.byType(Scrollable).first);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Encoding capacity & safety'));
    await tester.pumpAndSettle();

    expect(find.text('Simultaneous hardware transcodes'), findsOneWidget);
    expect(find.textContaining('CPU/software encoding always stays at one'),
        findsOneWidget);
    expect(find.text('Stop unexpectedly large outputs'), findsOneWidget);
  });

  testWidgets(
      'older server keeps the app connected and explains one limitation',
      (tester) async {
    final controller = AppController()..enterDemo();
    controller.serverSupportsOperationsSettings = false;
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    expect(find.text('Server request failed'), findsNothing);
    await tester.tap(find.byIcon(Icons.tune_outlined));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
        find.text('Encoding capacity & safety'), 220,
        scrollable: find.byType(Scrollable).first);
    await tester.tap(find.text('Encoding capacity & safety'));
    await tester.pumpAndSettle();

    expect(find.text('Server update required'), findsOneWidget);
    expect(find.textContaining('rest of ByteSqueeze remains connected'),
        findsOneWidget);
    expect(find.text('LEGACY SERVER'), findsOneWidget);
  });

  testWidgets('queue prioritizes running work and hardware capacity',
      (tester) async {
    final controller = AppController()..enterDemo();
    controller.statsForNerds = true;
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    await tester.tap(find.byIcon(Icons.motion_photos_on_outlined));
    await tester.pumpAndSettle();

    expect(find.text('Queue'), findsWidgets);
    expect(find.text('Running now'), findsOneWidget);
    expect(find.textContaining('GPU limit 2'), findsOneWidget);
    expect(find.text('Up next'), findsOneWidget);
  });

  testWidgets('combined queue identifies workers and edits queued presets',
      (tester) async {
    tester.view.physicalSize = const Size(800, 1200);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    final controller = AppController()..enterDemo();
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    await tester.tap(find.byIcon(Icons.motion_photos_on_outlined));
    await tester.pumpAndSettle();

    expect(find.textContaining('completed ·'), findsOneWidget);
    expect(find.textContaining('Office Encoder'), findsOneWidget);
    final menus = find.byType(PopupMenuButton<String>);
    expect(menus, findsWidgets);
    await tester.tap(menus.at(1));
    await tester.pumpAndSettle();
    expect(find.text('Edit preset'), findsOneWidget);
    await tester.tap(find.text('Edit preset'));
    await tester.pumpAndSettle();
    expect(find.text('Edit queued preset'), findsOneWidget);
    expect(find.text('Smart Preset'), findsOneWidget);
    expect(find.text('Automatic'), findsOneWidget);
    expect(find.text('1080p'), findsOneWidget);
    expect(find.text('4K'), findsOneWidget);
  });

  testWidgets('linked node page exposes worker controls', (tester) async {
    tester.view.physicalSize = const Size(800, 1200);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    final controller = AppController()..enterDemo();
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    await tester.tap(find.byIcon(Icons.tune_outlined));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('Encoding nodes'), 220,
        scrollable: find.byType(Scrollable).first);
    await tester.tap(find.text('Encoding nodes'));
    await tester.pumpAndSettle();

    expect(find.text('Living Room Mini PC'), findsOneWidget);
    expect(find.text('Office Encoder'), findsOneWidget);
    await tester.tap(find.byType(PopupMenuButton<String>).first);
    await tester.pumpAndSettle();
    expect(find.text('Refresh status'), findsOneWidget);
    expect(find.text('Rename worker'), findsOneWidget);
    expect(find.text('GPU capacity'), findsOneWidget);
    expect(find.text('Clear finished jobs'), findsOneWidget);
    expect(find.text('Unlink worker'), findsOneWidget);
  });

  testWidgets('library exposes friendly Smart preview and season actions',
      (tester) async {
    final controller = AppController()..enterDemo();
    controller.showSecondaryUi = true;
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    await tester.tap(find.descendant(
      of: find.byType(NavigationBar),
      matching: find.byIcon(Icons.video_library_outlined),
    ));
    await tester.pumpAndSettle();

    expect(find.text('Preview, tune, then queue'), findsNothing);
    expect(find.text('Best savings'), findsOneWidget);

    await tester.tap(find.text('Shows'));
    await tester.pumpAndSettle();
    final foundation = find.text('Foundation').first;
    await tester.scrollUntilVisible(foundation, 260,
        scrollable: find.byType(Scrollable).first);
    await tester.pumpAndSettle();
    await tester.tap(foundation);
    await tester.pumpAndSettle();

    expect(find.byType(DraggableScrollableSheet), findsOneWidget);
    final detailsScroll = find
        .descendant(
          of: find.byType(DraggableScrollableSheet),
          matching: find.byType(Scrollable),
        )
        .first;
    await tester.scrollUntilVisible(
        find.text('Preview first episode’s Smart encode'), 220,
        scrollable: detailsScroll);
    expect(find.text('Preview first episode’s Smart encode'), findsOneWidget);
    await tester.scrollUntilVisible(find.text('Queue destination'), 220,
        scrollable: detailsScroll);
    await tester.tap(find.text('This server (local)'));
    await tester.pumpAndSettle();
    expect(find.text('Next available · distribute files'), findsOneWidget);
    expect(find.text('Best node · keep selection together'), findsOneWidget);
    await tester.tap(find.text('Next available · distribute files'));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('Smart Queue Full Show'), 220,
        scrollable: detailsScroll);
    expect(find.text('Smart Queue Full Show'), findsOneWidget);
    await tester.scrollUntilVisible(
        find.text(
            'Preview one episode; queue planning remains independent for every episode'),
        220,
        scrollable: detailsScroll);
    expect(
        find.text(
            'Preview one episode; queue planning remains independent for every episode'),
        findsOneWidget);
    await tester.scrollUntilVisible(find.text('Season 1'), 220,
        scrollable: detailsScroll);
    expect(find.text('Season 1'), findsOneWidget);
  });

  testWidgets(
      'default UI is focused and optional detail is restored in settings',
      (tester) async {
    final controller = AppController()..enterDemo();
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    await tester.tap(find.byIcon(Icons.video_library_outlined));
    await tester.pumpAndSettle();
    expect(find.text('Preview, tune, then queue'), findsNothing);
    expect(find.text('Best savings'), findsOneWidget);

    await tester.tap(find.byIcon(Icons.tune_outlined));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('Interface'), 220,
        scrollable: find.byType(Scrollable).first);
    final interfaceTile = find.widgetWithText(ListTile, 'Interface');
    await tester.drag(find.byType(ListView).first, const Offset(0, -140));
    await tester.pumpAndSettle();
    await tester.tap(interfaceTile);
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('Show secondary controls'), 220,
        scrollable: find.byType(Scrollable).first);
    await tester.ensureVisible(find.text('Show secondary controls'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Show secondary controls'));
    await tester.ensureVisible(find.text('Stats for nerds'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Stats for nerds'));
    await tester.pumpAndSettle();

    expect(controller.showSecondaryUi, isTrue);
    expect(controller.statsForNerds, isTrue);
    expect(await controller.store.loadShowSecondaryUi(), isTrue);
    expect(await controller.store.loadStatsForNerds(), isTrue);
  });

  testWidgets('movie opens the native Size Wizard and learned queue flow',
      (tester) async {
    tester.view.physicalSize = const Size(800, 1200);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    final controller = AppController()..enterDemo();
    await tester.pumpWidget(ByteSqueezeApp(controller: controller));
    await tester.pumpAndSettle();

    await tester.tap(find.byIcon(Icons.video_library_outlined));
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(find.text('Arrival').last, 240,
        scrollable: find.byType(Scrollable).first);
    await tester.tap(find.text('Arrival').last);
    await tester.pumpAndSettle();
    final detailsScroll = find
        .descendant(
          of: find.byType(DraggableScrollableSheet),
          matching: find.byType(Scrollable),
        )
        .first;
    await tester.scrollUntilVisible(find.text('Open in Size Wizard'), 220,
        scrollable: detailsScroll);
    await tester.tap(find.text('Open in Size Wizard'));
    await tester.pumpAndSettle();

    expect(find.text('Size Wizard'), findsOneWidget);
    expect(find.textContaining('source FPS preserved'), findsOneWidget);
    expect(find.text('Keep quality'), findsOneWidget);
    expect(find.text('Balanced'), findsWidgets);
    expect(find.text('Save space'), findsOneWidget);
    expect(find.text('Queue encode'), findsOneWidget);
    final wizardScroll = find
        .descendant(
          of: find.byType(ListView).last,
          matching: find.byType(Scrollable),
        )
        .first;
    await tester.scrollUntilVisible(find.text('Queue destination'), 300,
        scrollable: wizardScroll);
    await tester.tap(find.text('Queue destination'));
    await tester.pumpAndSettle();
    expect(find.text('Next available node'), findsOneWidget);
    expect(find.text('Living Room Mini PC'), findsOneWidget);
    expect(find.text('Office Encoder'), findsOneWidget);
    await tester.tap(find.text('Living Room Mini PC'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Living Room Mini PC'), findsWidgets);
  });

  testWidgets('V3 stays usable across phone, foldable, tablet, and landscape',
      (tester) async {
    const sizes = [
      Size(360, 800),
      Size(390, 844),
      Size(430, 932),
      Size(650, 900),
      Size(768, 1024),
      Size(1024, 768),
    ];
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });

    for (final size in sizes) {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      final controller = AppController()..enterDemo();
      await tester.pumpWidget(ByteSqueezeApp(controller: controller));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull, reason: 'Overview at $size');

      for (var tab = 1; tab < 5; tab++) {
        controller.selectTab(tab);
        await tester.pumpAndSettle();
        expect(tester.takeException(), isNull, reason: 'Tab $tab at $size');
      }
    }
  });

  testWidgets('pairing is compact on narrow and large phones', (tester) async {
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });
    for (final size in const [Size(360, 800), Size(430, 932)]) {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      final controller = AppController()..booting = false;
      await tester.pumpWidget(ByteSqueezeApp(controller: controller));
      await tester.pumpAndSettle();
      expect(find.text('Connect to ByteSqueeze'), findsOneWidget);
      expect(find.text('Connect'), findsOneWidget);
      expect(tester.takeException(), isNull, reason: 'Pairing at $size');
    }
  });
}
