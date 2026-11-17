cmake_minimum_required(VERSION 3.16)
project(HandshakeTests LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
find_package(Qt6 REQUIRED COMPONENTS Widgets Charts)
# The emulators and the test binary are plain executables on every platform: the controller
# under test looks for handshake_v2/backend/SampleApl and handshake_v2/motor/RamanAutofocus
# (SampleApl.exe / RamanAutofocus.exe on Windows) next to its own executable.
# ../hardware holds only the three protocol headers these programs include (capture timing, scan
# handshake, line scan plan), unchanged from the original project; the Windows helper sources and
# the vendor libraries stay there, so motor_path_test below cannot be built from this folder.
add_executable(mock_backend mock_backend.cpp)
target_link_libraries(mock_backend PRIVATE Qt6::Core)
set_target_properties(mock_backend PROPERTIES OUTPUT_NAME SampleApl MACOSX_BUNDLE FALSE
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/handshake_v2/backend")
add_executable(mock_motor mock_motor.cpp)
set_target_properties(mock_motor PROPERTIES OUTPUT_NAME RamanAutofocus MACOSX_BUNDLE FALSE
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/handshake_v2/motor")
add_custom_command(OUTPUT "${CMAKE_BINARY_DIR}/main.moc"
    COMMAND Qt6::moc "${CMAKE_CURRENT_SOURCE_DIR}/../main.cpp" -o "${CMAKE_BINARY_DIR}/main.moc"
    DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/../main.cpp")
add_executable(handshake_tests handshake_tests.cpp "${CMAKE_BINARY_DIR}/main.moc")
target_compile_definitions(handshake_tests PRIVATE HANDSHAKE_TESTING)
if(MSVC)
    target_compile_options(handshake_tests PRIVATE /utf-8)
endif()
set_target_properties(handshake_tests PROPERTIES MACOSX_BUNDLE FALSE)
target_include_directories(handshake_tests PRIVATE "${CMAKE_BINARY_DIR}")
target_link_libraries(handshake_tests PRIVATE Qt6::Widgets Qt6::Charts)
add_dependencies(handshake_tests mock_backend mock_motor)
# The remembered Operator/Location exist only without HANDSHAKE_TESTING, so they are tested by a
# program of their own that compiles main.cpp the way the controller itself is compiled. It follows
# handshake_tests so that the shared main.moc is generated once.
add_executable(settings_tests settings_tests.cpp "${CMAKE_BINARY_DIR}/main.moc")
if(MSVC)
    target_compile_options(settings_tests PRIVATE /utf-8)
endif()
set_target_properties(settings_tests PROPERTIES MACOSX_BUNDLE FALSE)
target_include_directories(settings_tests PRIVATE "${CMAKE_BINARY_DIR}")
target_link_libraries(settings_tests PRIVATE Qt6::Widgets Qt6::Charts)
add_dependencies(settings_tests handshake_tests)
# The window with its Data and Review tabs. These two programs run from a folder of their own,
# ui_home/: the controller decides by its HOME folder whether the data layer is installed, and
# there data_layer/raman_catalog is the stand-in (fake_data_layer.py). The folder of handshake_tests
# has no data layer, so those tests keep running without any data-layer process.
find_package(Python3 COMPONENTS Interpreter)
if(Python3_Interpreter_FOUND)
    set(UI_HOME "${CMAKE_BINARY_DIR}/ui_home")
    set(UI_HOME_FILES
        "${UI_HOME}/data_layer/raman_catalog/__main__.py"
        "${UI_HOME}/handshake_v2/backend/SampleApl${CMAKE_EXECUTABLE_SUFFIX}"
        "${UI_HOME}/handshake_v2/motor/RamanAutofocus${CMAKE_EXECUTABLE_SUFFIX}")
    add_custom_command(OUTPUT "${UI_HOME}/data_layer/raman_catalog/__main__.py"
        COMMAND ${CMAKE_COMMAND} -E copy "${CMAKE_CURRENT_SOURCE_DIR}/fake_data_layer.py" "${UI_HOME}/data_layer/raman_catalog/__main__.py"
        DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/fake_data_layer.py")
    add_custom_command(OUTPUT "${UI_HOME}/handshake_v2/backend/SampleApl${CMAKE_EXECUTABLE_SUFFIX}"
        COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:mock_backend>" "${UI_HOME}/handshake_v2/backend/SampleApl${CMAKE_EXECUTABLE_SUFFIX}"
        DEPENDS mock_backend)
    add_custom_command(OUTPUT "${UI_HOME}/handshake_v2/motor/RamanAutofocus${CMAKE_EXECUTABLE_SUFFIX}"
        COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:mock_motor>" "${UI_HOME}/handshake_v2/motor/RamanAutofocus${CMAKE_EXECUTABLE_SUFFIX}"
        DEPENDS mock_motor)
    add_custom_target(ui_home DEPENDS ${UI_HOME_FILES})
    # ui_tests: the data-layer client, the status display and the two tabs. arc_ui_tests: the uploads
    # to ARC of the Data tab, against the stand-in's arc-* answers. ui_snapshot: PNG pictures of the
    # window in several states, in the light and the dark appearance, at two sizes.
    foreach(program ui_tests arc_ui_tests ui_snapshot)
        add_executable(${program} ${program}.cpp "${CMAKE_BINARY_DIR}/main.moc")
        target_compile_definitions(${program} PRIVATE HANDSHAKE_TESTING "UI_TEST_PYTHON=\"${Python3_EXECUTABLE}\"")
        if(MSVC)
            target_compile_options(${program} PRIVATE /utf-8)
        endif()
        set_target_properties(${program} PROPERTIES MACOSX_BUNDLE FALSE RUNTIME_OUTPUT_DIRECTORY "${UI_HOME}")
        target_include_directories(${program} PRIVATE "${CMAKE_BINARY_DIR}")
        target_link_libraries(${program} PRIVATE Qt6::Widgets Qt6::Charts)
        add_dependencies(${program} ui_home settings_tests)
    endforeach()
else()
    message(WARNING "No Python 3 interpreter found: ui_tests, arc_ui_tests and ui_snapshot, which start the stand-in data layer, are not built.")
endif()
if(WIN32)
    # Compiles the Windows motor program (RamanAutofocusTest.cpp: wmain, Kinesis C API) against
    # motor_sdk_stub.h, so this test exists only on Windows.
    add_executable(motor_path_test motor_path_test.cpp)
    target_compile_definitions(motor_path_test PRIVATE MOTOR_PATH_TESTING)
    if(MSVC)
        target_compile_options(motor_path_test PRIVATE /utf-8)
    endif()
    target_link_libraries(motor_path_test PRIVATE Qt6::Core)
endif()
