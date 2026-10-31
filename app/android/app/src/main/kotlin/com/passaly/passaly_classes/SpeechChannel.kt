package com.passaly.passaly_classes

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

/**
 * Live captions: this phone turns its own speaker's voice into text.
 *
 * Android's SpeechRecognizer stops after each utterance and its docs say it is
 * "not intended to be used for continuous recognition", so a class is a chain
 * of short sessions, restarted after every result, the same as Chrome does on
 * the web page. Costs battery for as long as captions are on.
 *
 * Offline recognition is asked for first (EXTRA_PREFER_OFFLINE, API 23), so a
 * child's voice stays on the phone where the language pack exists; when the
 * recognizer refuses, the chain carries on without it.
 *
 * Every call runs on the main thread, which the class requires.
 */
class SpeechChannel(private val activity: Activity) :
    MethodChannel.MethodCallHandler, EventChannel.StreamHandler, RecognitionListener {

    companion object {
        const val CHANNEL = "classes/speech"
        const val EVENTS = "classes/speech_events"
        const val PERMISSION_REQUEST = 7301
    }

    private var recognizer: SpeechRecognizer? = null
    private var sink: EventChannel.EventSink? = null
    private var wanted = false
    private var lang = "en-US"
    private var offline = true
    private var pendingStart: MethodChannel.Result? = null

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "available" -> result.success(SpeechRecognizer.isRecognitionAvailable(activity))
            "start" -> {
                lang = call.argument<String>("lang") ?: "en-US"
                if (activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    == PackageManager.PERMISSION_GRANTED) {
                    begin(); result.success(true)
                } else {
                    pendingStart = result
                    activity.requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), PERMISSION_REQUEST)
                }
            }
            "stop" -> { stop(); result.success(true) }
            else -> result.notImplemented()
        }
    }

    fun onPermissionResult(grants: IntArray) {
        val ok = grants.isNotEmpty() && grants[0] == PackageManager.PERMISSION_GRANTED
        if (ok) begin()
        pendingStart?.success(ok)
        pendingStart = null
    }

    private fun begin() {
        wanted = true
        offline = true
        if (recognizer == null) {
            recognizer = SpeechRecognizer.createSpeechRecognizer(activity).also {
                it.setRecognitionListener(this)
            }
        }
        listen()
    }

    private fun listen() {
        if (!wanted) return
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE, lang)
            .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            .putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, offline)
        recognizer?.startListening(intent)
    }

    fun stop() {
        wanted = false
        recognizer?.destroy()
        recognizer = null
    }

    private fun send(type: String, text: String = "") {
        sink?.success(mapOf("type" to type, "text" to text))
    }

    private fun best(b: Bundle?): String =
        b?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty().trim()

    override fun onPartialResults(b: Bundle?) = send("partial", best(b))

    override fun onResults(b: Bundle?) {
        val text = best(b)
        if (text.isNotEmpty()) send("final", text)
        listen()
    }

    override fun onError(error: Int) {
        when (error) {
            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> { stop(); send("error", "permission") }
            // Silence or a mumble ends a session; just start the next one.
            SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> listen()
            SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> {}
            else -> {
                if (offline) { offline = false; listen() }  // no offline pack: try the normal engine
                else { stop(); send("error", "code $error") }
            }
        }
    }

    override fun onListen(arguments: Any?, events: EventChannel.EventSink?) { sink = events }
    override fun onCancel(arguments: Any?) { sink = null }

    override fun onReadyForSpeech(params: Bundle?) = send("listening")
    override fun onBeginningOfSpeech() {}
    override fun onRmsChanged(rmsdB: Float) {}
    override fun onBufferReceived(buffer: ByteArray?) {}
    override fun onEndOfSpeech() {}
    override fun onEvent(eventType: Int, params: Bundle?) {}
}
