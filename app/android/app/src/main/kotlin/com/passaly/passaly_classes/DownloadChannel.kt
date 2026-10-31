package com.passaly.passaly_classes

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File

/**
 * Model downloads handed to Android's DownloadManager.
 *
 * Doing this in Dart worked only while the app stayed open on one screen. A
 * model is 0.9–2.6 GB, so a real download outlives the sheet that started it:
 * the user switches app, the screen locks, Android reclaims the process, and
 * an in-process download dies with no way to resume. DownloadManager survives
 * all of that, retries by itself, resumes rather than restarting, and puts
 * progress in the notification shade where a long download belongs.
 *
 * Files land in app-specific external storage — no permission needed, and
 * removed when the app is uninstalled, so a 2.6 GB model cannot outlive it.
 */
class DownloadChannel(private val context: Context) : MethodChannel.MethodCallHandler {

    companion object {
        const val CHANNEL = "classes/download"
        const val SUBDIR = "models"
    }

    private val manager: DownloadManager
        get() = context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "dir" -> result.success(dir().absolutePath)
            "enqueue" -> enqueue(call, result)
            "status" -> status(call.argument<Number>("id")?.toLong() ?: -1L, result)
            "cancel" -> {
                manager.remove(call.argument<Number>("id")?.toLong() ?: -1L)
                result.success(true)
            }
            else -> result.notImplemented()
        }
    }

    private fun dir(): File {
        val d = File(context.getExternalFilesDir(null), SUBDIR)
        if (!d.exists()) d.mkdirs()
        return d
    }

    private fun enqueue(call: MethodCall, result: MethodChannel.Result) {
        val url = call.argument<String>("url").orEmpty()
        val file = call.argument<String>("file").orEmpty()
        val title = call.argument<String>("title") ?: file
        val wifiOnly = call.argument<Boolean>("wifiOnly") ?: false
        if (url.isEmpty() || file.isEmpty()) {
            result.error("DL_ARGS", "url and file are required", null)
            return
        }

        // A leftover from a failed attempt would make DownloadManager append
        // "-1" to the name, and the app would then never find the model it
        // just downloaded.
        File(dir(), file).delete()

        try {
            val req = DownloadManager.Request(Uri.parse(url))
                .setTitle(title)
                .setDescription("Llama model for the class tutor")
                .setDestinationInExternalFilesDir(context, SUBDIR, file)
                .setNotificationVisibility(
                    DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED
                )
                .setAllowedOverMetered(!wifiOnly)
                .setAllowedOverRoaming(false)
            result.success(manager.enqueue(req))
        } catch (e: Throwable) {
            result.error("DL_ENQUEUE", "${e.javaClass.simpleName}: ${e.message}", null)
        }
    }

    /**
     * Progress for one download. Returns state "gone" when DownloadManager has
     * no record of the id, which happens after a reboot or if the user cleared
     * it from the shade — the caller treats that as "start again", not as an
     * error worth showing.
     */
    private fun status(id: Long, result: MethodChannel.Result) {
        val query = DownloadManager.Query().setFilterById(id)
        manager.query(query).use { c ->
            if (c == null || !c.moveToFirst()) {
                result.success(mapOf("state" to "gone"))
                return
            }
            val status = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))
            val done = c.getLong(
                c.getColumnIndexOrThrow(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR)
            )
            val total = c.getLong(
                c.getColumnIndexOrThrow(DownloadManager.COLUMN_TOTAL_SIZE_BYTES)
            )
            val state = when (status) {
                DownloadManager.STATUS_SUCCESSFUL -> "done"
                DownloadManager.STATUS_FAILED -> "failed"
                DownloadManager.STATUS_PAUSED -> "paused"
                else -> "running"
            }
            val out = mutableMapOf<String, Any>(
                "state" to state,
                "downloaded" to done,
                "total" to total,
            )
            if (status == DownloadManager.STATUS_FAILED) {
                out["reason"] =
                    c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_REASON))
            }
            result.success(out)
        }
    }
}
