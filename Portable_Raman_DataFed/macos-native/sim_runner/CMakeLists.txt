cmake_minimum_required(VERSION 3.16)
project(RamanSimRunner LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
find_package(Qt6 REQUIRED COMPONENTS Widgets Charts)
# The runner compiles the controller's own source (laser_test/main.cpp) the way the end-to-end test
# does: main.cpp ends with #include "main.moc", which is generated here.
set(CONTROLLER_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../laser_test")
add_custom_command(OUTPUT "${CMAKE_BINARY_DIR}/main.moc"
    COMMAND Qt6::moc "${CONTROLLER_DIR}/main.cpp" -o "${CMAKE_BINARY_DIR}/main.moc"
    DEPENDS "${CONTROLLER_DIR}/main.cpp")
add_executable(raman_sim_runner raman_sim_runner.cpp "${CMAKE_BINARY_DIR}/main.moc")
set_target_properties(raman_sim_runner PROPERTIES MACOSX_BUNDLE FALSE)
target_include_directories(raman_sim_runner PRIVATE "${CMAKE_BINARY_DIR}" "${CONTROLLER_DIR}")
target_link_libraries(raman_sim_runner PRIVATE Qt6::Widgets Qt6::Charts)
