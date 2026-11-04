package com.passaly.passaly_classes

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.provider.CalendarContract
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
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
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
}
