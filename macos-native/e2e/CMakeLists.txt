cmake_minimum_required(VERSION 3.16)
project(RamanE2E LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
find_package(Qt6 REQUIRED COMPONENTS Widgets Charts)
set(CONTROLLER_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../laser_test")
add_custom_command(OUTPUT "${CMAKE_BINARY_DIR}/main.moc"
    COMMAND Qt6::moc "${CONTROLLER_DIR}/main.cpp" -o "${CMAKE_BINARY_DIR}/main.moc"
    DEPENDS "${CONTROLLER_DIR}/main.cpp")
add_executable(e2e_simulated_test e2e_simulated_test.cpp "${CMAKE_BINARY_DIR}/main.moc")
target_include_directories(e2e_simulated_test PRIVATE "${CMAKE_BINARY_DIR}" "${CONTROLLER_DIR}")
target_link_libraries(e2e_simulated_test PRIVATE Qt6::Widgets Qt6::Charts)
