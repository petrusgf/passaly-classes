package com.passaly.passaly_classes

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.provider.CalendarContract
import android.provider.OpenableColumns
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

/**
 * The assistant's tasks on this phone. Each one opens the app that does it
 * (the calendar's new-event screen, the share sheet) already filled in, so
 * the user sees and confirms it there: this app writes nothing itself and
 * needs no calendar permission.
 *
 * Calendar: ACTION_INSERT on Events.CONTENT_URI with EXTRA_EVENT_BEGIN_TIME /
 * EXTRA_EVENT_END_TIME in epoch milliseconds (developer.android.com/guide/
 * components/intents-common#AddEvent). The docs check resolveActivity first,
 * which on Android 11+ needs a <queries> entry; catching
 * ActivityNotFoundException gives the same answer without one.
 */
class ActionsChannel(private val activity: Activity) : MethodChannel.MethodCallHandler {

    companion object {
        const val CHANNEL = "classes/actions"
        const val PICK_REQUEST = 7302
        const val MAX_BYTES = 10 * 1024 * 1024  // the server's limit
    }

    private var pending: MethodChannel.Result? = null

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        if (call.method == "pickFile") return pickFile(result)
        val intent = when (call.method) {
            "calendar" -> Intent(Intent.ACTION_INSERT)
                .setData(CalendarContract.Events.CONTENT_URI)
                .putExtra(CalendarContract.Events.TITLE, call.argument<String>("title"))
                .putExtra(CalendarContract.Events.DESCRIPTION, call.argument<String>("description"))
                .putExtra(CalendarContract.EXTRA_EVENT_BEGIN_TIME, call.argument<Number>("begin")?.toLong())
                .putExtra(CalendarContract.EXTRA_EVENT_END_TIME, call.argument<Number>("end")?.toLong())
            "share" -> Intent.createChooser(
                Intent(Intent.ACTION_SEND).setType("text/plain")
                    .putExtra(Intent.EXTRA_TEXT, call.argument<String>("text")),
                null,
            )
            else -> return result.notImplemented()
        }
        try {
            activity.startActivity(intent)
            result.success(true)
        } catch (e: ActivityNotFoundException) {
            result.success(false)  // no calendar app on this phone
        }
    }

    /**
     * The system file picker (ACTION_OPEN_DOCUMENT, developer.android.com/training/
     * data-storage/shared/documents-files): the user grants this one file, so no
     * storage permission. Answers {name, bytes}, or null when they back out.
     */
    private fun pickFile(result: MethodChannel.Result) {
        pending?.success(null)
        pending = result
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            .addCategory(Intent.CATEGORY_OPENABLE)
            .setType("*/*")
            .putExtra(Intent.EXTRA_MIME_TYPES, arrayOf("application/pdf", "text/plain", "text/markdown"))
        activity.startActivityForResult(intent, PICK_REQUEST)
    }

    fun onPicked(uri: Uri?) {
        val result = pending ?: return
        pending = null
        if (uri == null) return result.success(null)
        try {
            val resolver = activity.contentResolver
            val name = resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
                if (it.moveToFirst()) it.getString(0) else null
            } ?: "file"
            // Read one byte past the limit, so "too big" is known without reading all of it.
            val bytes = resolver.openInputStream(uri)?.use { input ->
                val buf = ByteArray(MAX_BYTES + 1)
                var n = 0
                while (n < buf.size) {
                    val r = input.read(buf, n, buf.size - n)
                    if (r < 0) break
                    n += r
                }
                buf.copyOf(n)
            } ?: return result.error("READ", "Could not open the file", null)
            if (bytes.size > MAX_BYTES) return result.error("TOO_BIG", "Files can be up to 10 MB", null)
            result.success(mapOf("name" to name, "bytes" to bytes))
        } catch (e: Exception) {
            result.error("READ", e.message ?: "Could not read the file", null)
        }
    }
}
