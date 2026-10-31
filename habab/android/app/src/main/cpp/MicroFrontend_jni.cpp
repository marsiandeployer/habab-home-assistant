#include <jni.h>

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <vector>

extern "C" {
#include "tensorflow/lite/experimental/microfrontend/lib/frontend.h"
#include "tensorflow/lite/experimental/microfrontend/lib/frontend_util.h"
}

namespace {
constexpr int kSamplesPerStep = 160;
constexpr int kFeatureCount = 40;
constexpr float kFeatureScale = 0.0390625f;

struct Frontend {
  FrontendConfig config{};
  FrontendState state{};
  std::vector<int16_t> pending;

  Frontend() {
    config.window.size_ms = 30;
    config.window.step_size_ms = 10;
    config.filterbank.num_channels = kFeatureCount;
    config.filterbank.lower_band_limit = 125.0f;
    config.filterbank.upper_band_limit = 7500.0f;
    config.noise_reduction.smoothing_bits = 10;
    config.noise_reduction.even_smoothing = 0.025f;
    config.noise_reduction.odd_smoothing = 0.06f;
    config.noise_reduction.min_signal_remaining = 0.05f;
    config.pcan_gain_control.enable_pcan = 1;
    config.pcan_gain_control.strength = 0.95f;
    config.pcan_gain_control.offset = 80.0f;
    config.pcan_gain_control.gain_bits = 21;
    config.log_scale.enable_log = 1;
    config.log_scale.scale_shift = 6;
    FrontendPopulateState(&config, &state, 16000);
    pending.reserve(kSamplesPerStep);
  }

  ~Frontend() {
    std::fill(pending.begin(), pending.end(), 0);
    FrontendFreeStateContents(&state);
  }

  void Reset() {
    FrontendFreeStateContents(&state);
    FrontendPopulateState(&config, &state, 16000);
    std::fill(pending.begin(), pending.end(), 0);
    pending.clear();
  }
};

void Throw(JNIEnv* env, const char* message) {
  jclass type = env->FindClass("java/lang/IllegalStateException");
  if (type != nullptr) env->ThrowNew(type, message);
}
}  // namespace

extern "C" JNIEXPORT jlong JNICALL
Java_ru_habab_home_MicroFrontend_nativeCreate(JNIEnv*, jclass) {
  auto* frontend = new (std::nothrow) Frontend();
  return reinterpret_cast<jlong>(frontend);
}

extern "C" JNIEXPORT jfloatArray JNICALL
Java_ru_habab_home_MicroFrontend_nativeProcess(JNIEnv* env, jclass,
                                                jlong handle,
                                                jshortArray input,
                                                jint count) {
  auto* frontend = reinterpret_cast<Frontend*>(handle);
  if (frontend == nullptr || input == nullptr || count <= 0 ||
      count > env->GetArrayLength(input)) {
    Throw(env, "invalid_microfrontend_input");
    return nullptr;
  }

  const jsize old_pending = static_cast<jsize>(frontend->pending.size());
  frontend->pending.resize(old_pending + count);
  env->GetShortArrayRegion(input, 0, count, frontend->pending.data() + old_pending);
  if (env->ExceptionCheck()) {
    std::fill(frontend->pending.begin() + old_pending, frontend->pending.end(), 0);
    frontend->pending.resize(old_pending);
    return nullptr;
  }

  std::vector<jfloat> output;
  size_t consumed = 0;
  while (frontend->pending.size() - consumed >= kSamplesPerStep) {
    size_t samples_read = 0;
    FrontendOutput frame = FrontendProcessSamples(
        &frontend->state, frontend->pending.data() + consumed,
        kSamplesPerStep, &samples_read);
    if (samples_read != kSamplesPerStep ||
        (frame.size != 0 && (frame.size != kFeatureCount || frame.values == nullptr))) {
      Throw(env, "invalid_microfrontend_output");
      return nullptr;
    }
    for (size_t i = 0; i < frame.size; ++i)
      output.push_back(static_cast<jfloat>(frame.values[i] * kFeatureScale));
    consumed += samples_read;
  }
  if (consumed != 0) {
    const size_t remaining = frontend->pending.size() - consumed;
    std::move(frontend->pending.begin() + consumed, frontend->pending.end(),
              frontend->pending.begin());
    std::fill(frontend->pending.begin() + remaining, frontend->pending.end(), 0);
    frontend->pending.resize(remaining);
  }

  jfloatArray result = env->NewFloatArray(static_cast<jsize>(output.size()));
  if (result != nullptr && !output.empty())
    env->SetFloatArrayRegion(result, 0, static_cast<jsize>(output.size()), output.data());
  std::fill(output.begin(), output.end(), 0.0f);
  return result;
}

extern "C" JNIEXPORT void JNICALL
Java_ru_habab_home_MicroFrontend_nativeReset(JNIEnv* env, jclass, jlong handle) {
  auto* frontend = reinterpret_cast<Frontend*>(handle);
  if (frontend == nullptr) {
    Throw(env, "microfrontend_closed");
    return;
  }
  frontend->Reset();
}

extern "C" JNIEXPORT void JNICALL
Java_ru_habab_home_MicroFrontend_nativeClose(JNIEnv*, jclass, jlong handle) {
  delete reinterpret_cast<Frontend*>(handle);
}
