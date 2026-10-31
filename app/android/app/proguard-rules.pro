# R8 runs on Flutter release builds by default, and on-device inference does
# not survive it unaided: the engine config is a protobuf whose fields the
# native layer looks up reflectively, by name. Renaming them turns into
#
#   GEMMA_LOAD: RuntimeException: Field modelPath_ for r2.g not found
#
# at load time — a release-only failure that every debug build hides.

# Protobuf keeps its state in fields named like modelPath_ and reads them
# reflectively, so the names have to survive.
-keep class com.google.protobuf.** { *; }
-keepclassmembers class * extends com.google.protobuf.GeneratedMessageLite {
    <fields>;
    <methods>;
}

# LiteRT-LM: the JNI layer resolves these from native code, which R8 cannot
# see, and its config objects are read reflectively the same way.
-keep class com.google.ai.edge.litertlm.** { *; }
-keep interface com.google.ai.edge.litertlm.** { *; }

# JNI calls back into these; R8 cannot see the call sites.
-keepclasseswithmembernames class * {
    native <methods>;
}

-dontwarn com.google.ai.edge.litertlm.**
-dontwarn com.google.protobuf.**

# ML Kit GenAI (Gemini Nano through AICore). This path works today, but it
# resolves classes reflectively as well, and it is one release-build change
# away from failing the same way.
-keep class com.google.mlkit.genai.** { *; }
-dontwarn com.google.mlkit.genai.**
