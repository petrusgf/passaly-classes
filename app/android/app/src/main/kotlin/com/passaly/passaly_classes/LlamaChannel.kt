package com.passaly.passaly_classes

import android.app.ActivityManager
import android.content.Context
import android.os.Handler
import android.os.Looper
import com.google.ai.edge.litertlm.Backend
import com.google.ai.edge.litertlm.Engine
import com.google.ai.edge.litertlm.EngineConfig
import com.google.ai.edge.litertlm.Content
import com.google.ai.edge.litertlm.Conversation
import com.google.ai.edge.litertlm.ConversationConfig
import com.google.ai.edge.litertlm.Message
import com.google.ai.edge.litertlm.MessageCallback
import com.google.ai.edge.litertlm.SamplerConfig
import com.google.ai.edge.litertlm.ThinkingConfig
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors

/**
 * Meta's Llama 3.2 running in our own process, via LiteRT-LM.
 *
 * Copied from the Passaly app's GemmaChannel, which has been through the
 * runtime's traps already (wrong runtime, chat template, sampler, streaming).
 * Only the model and the backend fallback differ.
 */
class LlamaChannel(private val context: Context) :
    MethodChannel.MethodCallHandler, EventChannel.StreamHandler {

    companion object {
        const val CHANNEL = "classes/llama"

        /** Partial text as it is generated — see [generate]. */
        const val EVENTS = "classes/llama_events"

        /**
         * Input *and* output together — the runtime counts both against it.
         * At 2048 a tutor prompt (~970 tokens of question, explanation and
         * retrieved material) plus four turns of history overflowed mid-answer
         * with OUT_OF_RANGE. Both published models carry 4096.
         */
        private const val MAX_TOKENS = 4096

        /**
         * Ceiling on the answer, so input + output cannot exceed [MAX_TOKENS].
         * A tutor explaining one exam question has no business writing more
         * than a few paragraphs, and every extra token is seconds of CPU.
         */
        private const val MAX_OUTPUT_TOKENS = 512

        /**
         * Roughly "6 GB or more", since a 6 GB device reports about 5.6 GB.
         *
         * Weights plus KV cache, against an Android that kills the app long
         * before the device is truly out. A 4 GB phone clears a lower floor on
         * paper and then loses the race against the launcher for what is
         * actually free — costing the user the whole download first.
         *
         * ponytail: a static floor, not a free-memory check. availMem swings
         * with whatever else is open, so it would admit or refuse the same
         * phone depending on the minute. Per-model requirements live in the
         * server manifest, which is what actually decides what is offered.
         */
        private const val MIN_RAM_BYTES = 5_300_000_000L
    }

    private val main = Handler(Looper.getMainLooper())

    /**
     * Single thread on purpose: initialising takes seconds and generating
     * takes longer, both block, and neither may touch the main thread.
     * Serialising them also removes the "generate while still loading" race.
     */
    private val worker = Executors.newSingleThreadExecutor()

    @Volatile private var engine: Engine? = null

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "canRun" -> result.success(totalRam() >= MIN_RAM_BYTES)
            "ramBytes" -> result.success(totalRam())
            "isLoaded" -> result.success(engine != null)
            "load" -> load(call.argument<String>("path").orEmpty(), result)
            "generate" -> generate(call.argument<String>("prompt").orEmpty(), result)
            "cancel" -> { current?.cancelProcess(); result.success(true) }
            "unload" -> { unload(); result.success(true) }
            else -> result.notImplemented()
        }
    }

    private fun totalRam(): Long {
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val info = ActivityManager.MemoryInfo()
        am.getMemoryInfo(info)
        return info.totalMem
    }

    private fun load(path: String, result: MethodChannel.Result) {
        if (engine != null) { result.success(true); return }
        if (path.isEmpty() || !File(path).exists()) {
            result.error("LLAMA_NO_MODEL", "no model file at '$path'", null)
            return
        }
        worker.execute {
            try {
                // CPU first, as in Passaly: GPU fails at load on a long tail
                // of drivers. But the Llama files are published as "_gpu"
                // builds, so if CPU refuses the file, GPU gets one try.
                // ponytail: no per-device memory of which backend worked.
                engine = try {
                    start(path, Backend.CPU())
                } catch (cpu: Throwable) {
                    try { start(path, Backend.GPU()) } catch (gpu: Throwable) { throw cpu }
                }
                main.post { result.success(true) }
            } catch (e: Throwable) {
                main.post { result.error("LLAMA_LOAD", describe(e), null) }
            }
        }
    }

    private fun start(path: String, backend: Backend): Engine {
        val created = Engine(
            EngineConfig(
                modelPath = path,
                backend = backend,
                maxNumTokens = MAX_TOKENS,
                cacheDir = context.cacheDir.absolutePath,
            )
        )
        created.initialize()
        return created
    }

    private var events: EventChannel.EventSink? = null

    @Volatile private var current: Conversation? = null

    override fun onListen(arguments: Any?, sink: EventChannel.EventSink?) {
        events = sink
    }

    override fun onCancel(arguments: Any?) {
        events = null
    }

    /** The text carried by a message, ignoring tool calls and other parts. */
    private fun textOf(m: Message): String =
        m.contents.contents.filterIsInstance<Content.Text>().joinToString("") { it.text }

    /**
     * Streams the answer, and returns the whole of it when done.
     *
     * Goes through a Conversation rather than a raw Session because that is
     * what applies the model's chat template. Feeding a bare completion prompt
     * to an instruction-tuned model produced fluent multilingual nonsense with
     * `<turn|>` markers embedded in it — the model continuing a document
     * instead of answering a question.
     *
     * Streaming is not a flourish either. A ~970-token prompt has to be read
     * before the first word exists, and on a phone CPU a large model can spend
     * minutes there — indistinguishable, from outside, from a hang.
     */
    private fun generate(prompt: String, result: MethodChannel.Result) {
        val e = engine
        if (e == null) {
            result.error("LLAMA_NOT_LOADED", "load the model before generating", null)
            return
        }
        worker.execute {
            // One conversation per question: the tutor re-sends its own
            // history inside the prompt, so a conversation that outlived the
            // call would carry every turn twice.
            var conv: Conversation? = null
            try {
                conv = e.createConversation(
                    ConversationConfig(
                        thinkingConfig = ThinkingConfig(false),
                        maxOutputToken = MAX_OUTPUT_TOKENS,
                        // Without one the runtime decodes greedily, and a
                        // 1.7B model on greedy restates the question in a
                        // loop until the output cap cuts it off. Values
                        // kept from the Passaly app; not tuned for Llama.
                        samplerConfig = SamplerConfig(
                            topK = 40,
                            topP = 0.95,
                            temperature = 0.7,
                        ),
                    )
                )
                current = conv

                val finished = CountDownLatch(1)
                var failure: Throwable? = null
                var text = ""

                conv.sendMessageAsync(
                    prompt,
                    object : MessageCallback {
                        override fun onMessage(message: Message) {
                            val incoming = textOf(message)
                            // The callback may deliver deltas or a growing
                            // snapshot, and the two are indistinguishable
                            // without checking: appending a snapshot repeats
                            // the answer, replacing with a delta loses it.
                            val delta = if (incoming.startsWith(text)) {
                                incoming.substring(text.length)
                            } else {
                                incoming
                            }
                            if (delta.isEmpty()) return
                            text += delta
                            main.post { events?.success(delta) }
                        }

                        override fun onDone() = finished.countDown()

                        override fun onError(t: Throwable) {
                            failure = t
                            finished.countDown()
                        }
                    },
                )
                finished.await()

                val err = failure
                if (err != null) {
                    main.post { result.error("LLAMA_GENERATE", describe(err), null) }
                } else {
                    main.post { result.success(text) }
                }
            } catch (t: Throwable) {
                main.post { result.error("LLAMA_GENERATE", describe(t), null) }
            } finally {
                current = null
                runCatching { conv?.close() }
            }
        }
    }

    /** Frees about a gigabyte. Worth doing when the user leaves a test. */
    fun unload() {
        val e = engine ?: return
        engine = null
        worker.execute { runCatching { e.close() } }
    }

    private fun describe(t: Throwable): String =
        "${t.javaClass.simpleName}: ${t.message ?: "(no message)"}"
}
