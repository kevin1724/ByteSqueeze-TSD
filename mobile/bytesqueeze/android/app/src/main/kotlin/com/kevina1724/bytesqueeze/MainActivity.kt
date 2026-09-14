package com.kevina1724.bytesqueeze

import android.content.Intent
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private val linksChannelName = "com.kevina1724.bytesqueeze/links"
    private var linksChannel: MethodChannel? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        linksChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, linksChannelName)
        linksChannel?.setMethodCallHandler { call, result ->
            if (call.method == "getInitialLink") {
                result.success(intent?.dataString)
            } else {
                result.notImplemented()
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        intent.dataString?.let { linksChannel?.invokeMethod("pairingLink", it) }
    }
}
