package com.passaly.passaly_classes

import android.content.Intent
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private var llama: LlamaChannel? = null
    private var speech: SpeechChannel? = null
    private var actions: ActionsChannel? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        val messenger = flutterEngine.dartExecutor.binaryMessenger

        // applicationContext, not the activity: the loaded model outlives a
        // rotation, and an Activity held for the life of a 1 GB model leaks.
        llama = LlamaChannel(applicationContext).also {
            MethodChannel(messenger, LlamaChannel.CHANNEL).setMethodCallHandler(it)
            EventChannel(messenger, LlamaChannel.EVENTS).setStreamHandler(it)
        }
        MethodChannel(messenger, DownloadChannel.CHANNEL)
            .setMethodCallHandler(DownloadChannel(applicationContext))

        // The activity: starting another app (calendar, share sheet) needs one.
        actions = ActionsChannel(this).also {
            MethodChannel(messenger, ActionsChannel.CHANNEL).setMethodCallHandler(it)
        }

        // The activity: asking for the microphone needs one.
        speech = SpeechChannel(this).also {
            MethodChannel(messenger, SpeechChannel.CHANNEL).setMethodCallHandler(it)
            EventChannel(messenger, SpeechChannel.EVENTS).setStreamHandler(it)
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == SpeechChannel.PERMISSION_REQUEST) speech?.onPermissionResult(grantResults)
    }

    @Deprecated("Activity result API needs AndroidX Activity; this is the plain-Activity path")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == ActionsChannel.PICK_REQUEST) {
            actions?.onPicked(if (resultCode == RESULT_OK) data?.data else null)
        }
    }

    override fun onDestroy() {
        llama?.unload()
        speech?.stop()
        super.onDestroy()
    }
}
