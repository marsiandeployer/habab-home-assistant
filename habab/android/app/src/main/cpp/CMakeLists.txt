cmake_minimum_required(VERSION 3.22.1)
project(microfrontend LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

set(MICRO_LIB microfrontend/tensorflow/lite/experimental/microfrontend/lib)
add_library(microfrontend STATIC
    ${MICRO_LIB}/fft.cc
    ${MICRO_LIB}/fft_util.cc
    ${MICRO_LIB}/kiss_fft_int16.cc
    ${MICRO_LIB}/filterbank.cc
    ${MICRO_LIB}/filterbank_util.cc
    ${MICRO_LIB}/frontend.cc
    ${MICRO_LIB}/frontend_util.cc
    ${MICRO_LIB}/log_lut.cc
    ${MICRO_LIB}/log_scale.cc
    ${MICRO_LIB}/log_scale_util.cc
    ${MICRO_LIB}/noise_reduction.cc
    ${MICRO_LIB}/noise_reduction_util.cc
    ${MICRO_LIB}/pcan_gain_control.cc
    ${MICRO_LIB}/pcan_gain_control_util.cc
    ${MICRO_LIB}/window.cc
    ${MICRO_LIB}/window_util.cc)
target_include_directories(microfrontend PUBLIC microfrontend microfrontend/kissfft)
target_compile_definitions(microfrontend PRIVATE FIXED_POINT=16)

if(ANDROID)
    add_library(microfrontend_jni SHARED MicroFrontend_jni.cpp)
    target_include_directories(microfrontend_jni PRIVATE microfrontend)
    target_link_libraries(microfrontend_jni PRIVATE microfrontend log)
else()
    include(CTest)
    if(BUILD_TESTING)
        add_executable(microfrontend_golden_test microfrontend_golden_test.cpp)
        target_link_libraries(microfrontend_golden_test PRIVATE microfrontend)
        add_test(NAME microfrontend_golden_test COMMAND microfrontend_golden_test)
    endif()
endif()
