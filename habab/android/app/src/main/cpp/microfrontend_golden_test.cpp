#include <cmath>
#include <cstdio>
#include <vector>

extern "C" {
#include "tensorflow/lite/experimental/microfrontend/lib/frontend.h"
#include "tensorflow/lite/experimental/microfrontend/lib/frontend_util.h"
}

namespace {
constexpr float kScale = 0.0390625f;
constexpr float kExpectedTail[80] = {
    13.125f, 9.21875f, 15.8984375f, 12.6953125f, 13.125f, 0.f, 13.7109375f, 0.f,
    0.f, 0.f, 0.f, 0.f, 15.625f, 12.578125f, 16.40625f, 6.9140625f,
    0.f, 0.f, 12.265625f, 12.265625f, 16.25f, 0.f, 0.f, 5.1953125f,
    16.1328125f, 7.9296875f, 0.f, 7.9296875f, 15.5859375f, 0.f, 14.140625f, 10.9375f,
    5.1953125f, 9.6875f, 13.359375f, 0.f, 14.4921875f, 0.f, 14.0234375f, 6.9140625f,
    16.953125f, 6.9140625f, 0.f, 5.1953125f, 12.265625f, 0.f, 12.421875f, 0.f,
    16.875f, 12.6953125f, 6.9140625f, 11.40625f, 16.796875f, 0.f, 16.0546875f, 10.9375f,
    12.6953125f, 12.8125f, 11.6015625f, 12.421875f, 13.125f, 12.265625f, 14.4140625f, 9.21875f,
    15.46875f, 9.21875f, 15.234375f, 10.390625f, 14.765625f, 10.703125f,
    15.7421875f, 9.6875f, 15.078125f, 9.6875f, 15.3125f, 9.6875f,
    15.0390625f, 10.078125f, 15.1171875f, 10.078125f};

std::vector<float> RunSequence() {
  FrontendConfig config{};
  config.window.size_ms = 30;
  config.window.step_size_ms = 10;
  config.filterbank.num_channels = 40;
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
  FrontendState state{};
  FrontendPopulateState(&config, &state, 16000);

  auto process = [&state]() {
    std::vector<float> features;
    int16_t samples[160];
    constexpr double pi = 3.14159265358979323846;
    for (int start = 0; start < 6400; start += 160) {
      for (int i = 0; i < 160; ++i) {
        const int n = start + i;
        if (n % 193 == 0) samples[i] = 32767;
        else if (n % 271 == 0) samples[i] = -32768;
        else samples[i] = static_cast<int16_t>(std::lround(std::sin(2 * pi * 440 * n / 16000) * 12000));
      }
      size_t consumed = 0;
      FrontendOutput frame = FrontendProcessSamples(&state, samples, 160, &consumed);
      if (consumed != 160 || (frame.size != 0 && (frame.size != 40 || frame.values == nullptr))) {
        std::fprintf(stderr, "invalid frame consumed=%zu size=%zu values=%p\n", consumed, frame.size, static_cast<const void*>(frame.values));
        return std::vector<float>{};
      }
      for (size_t i = 0; i < frame.size; ++i) features.push_back(frame.values[i] * kScale);
    }
    return features;
  };
  const std::vector<float> first = process();
  FrontendFreeStateContents(&state);
  FrontendPopulateState(&config, &state, 16000);
  const std::vector<float> after_reset = process();
  FrontendFreeStateContents(&state);
  if (first != after_reset) {
    std::fprintf(stderr, "frontend reset did not reproduce the feature stream\n");
    return {};
  }
  return after_reset;
}
}  // namespace

int main() {
  const std::vector<float> first = RunSequence();
  if (first.size() != 38 * 40) return 1;
  for (size_t i = 0; i < 80; ++i)
    if (std::fabs(first[first.size() - 80 + i] - kExpectedTail[i]) > 0.0001f) {
      std::fprintf(stderr, "frontend golden mismatch at %zu: got %.8f expected %.8f\n",
                   i, first[first.size() - 80 + i], kExpectedTail[i]);
      return 3;
    }
  return 0;
}
