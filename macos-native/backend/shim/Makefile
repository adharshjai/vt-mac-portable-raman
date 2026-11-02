# Builds libwinusb_shim.dylib (the Windows USB API shim for wrrusb2a.dll, see
# PORT_SPEC.md section 5) and shim_selftest. Both must compile with zero
# warnings under -Wall -Wextra.
#
#   make            build the dylib and the self-test
#   make test       build, run ./shim_selftest, list the exports with nm
#   make clean
#
# Variables (override on the command line if libusb lives elsewhere):
#   LIBUSB_CFLAGS, LIBUSB_LIBS, LIBUSB_STATIC_LIB, CC, ARCH
#   STATIC_LIBUSB=1  link libusb statically so the dylib has no Homebrew
#                    runtime dependency. This is the DEFAULT whenever the
#                    static archive exists: the deploy folder (PORT_SPEC
#                    section 1) carries only libwinusb_shim.dylib next to
#                    SampleApl.dll, so the shim must be self-contained.
#   STATIC_LIBUSB=0  link /opt/homebrew/lib/libusb-1.0.dylib dynamically
#                    instead (the dylib then needs Homebrew libusb at run time).

CC                := clang
ARCH              := arm64
LIBUSB_CFLAGS     ?= -I/opt/homebrew/include/libusb-1.0
LIBUSB_STATIC_LIB ?= /opt/homebrew/lib/libusb-1.0.a
STATIC_LIBUSB     ?= $(if $(wildcard $(LIBUSB_STATIC_LIB)),1,0)
ifeq ($(STATIC_LIBUSB),1)
LIBUSB_LIBS       ?= $(LIBUSB_STATIC_LIB) -framework IOKit -framework CoreFoundation -framework Security -lobjc
else
LIBUSB_LIBS       ?= -L/opt/homebrew/lib -lusb-1.0
endif
WARNFLAGS         := -Wall -Wextra
CFLAGS            ?= -O2
DYLIB             := libwinusb_shim.dylib
SELFTEST          := shim_selftest

# Every entry point the DLL imports (wrrusb2a_refs.txt "P/Invoke (ImplMap)")
# must be a global defined symbol of the dylib. "#2" and "#4" are the shell32
# ordinal entry points; dlsym(3) on macOS prepends the underscore itself.
EXPORTS := CreateFile CloseHandle QueryPerformanceCounter QueryPerformanceFrequency \
           GetPrivateProfileString RegisterDeviceNotification UnregisterDeviceNotification \
           SHChangeNotifyRegister SHChangeNotifyUnregister '\#2' '\#4' \
           SetupDiCreateDeviceInfoList SetupDiGetClassDevs SetupDiEnumDeviceInterfaces \
           SetupDiGetDeviceInterfaceDetail SetupDiDestroyDeviceInfoList \
           WinUsb_Initialize WinUsb_Free WinUsb_GetDescriptor WinUsb_QueryInterfaceSettings \
           WinUsb_QueryPipe WinUsb_QueryDeviceInformation WinUsb_SetPipePolicy \
           WinUsb_ReadPipe WinUsb_WritePipe WinUsb_ControlTransfer WinUsb_ResetPipe

# Only the entry points are exported: libusb's own symbols stay private to the
# dylib when the archive is linked in.
EXPORT_FLAGS := $(foreach s,$(EXPORTS),-Wl,-exported_symbol,_$(s))

.PHONY: all test exports clean

all: $(DYLIB) $(SELFTEST)

$(DYLIB): winusb_shim.c winusb_shim.h
	$(CC) -arch $(ARCH) -shared -fPIC $(WARNFLAGS) $(CFLAGS) $(LIBUSB_CFLAGS) winusb_shim.c \
	    -o $@ $(LIBUSB_LIBS) -Wl,-install_name,@rpath/$(DYLIB) $(EXPORT_FLAGS)

$(SELFTEST): shim_selftest.c winusb_shim.h $(DYLIB)
	$(CC) -arch $(ARCH) $(WARNFLAGS) $(CFLAGS) shim_selftest.c -o $@ \
	    -L. -lwinusb_shim -Wl,-rpath,@loader_path

test: all
	./$(SELFTEST)
	$(MAKE) exports

exports: $(DYLIB)
	@nm -gU $(DYLIB)
	@missing=0; for s in $(EXPORTS); do \
	    if ! nm -gU $(DYLIB) | grep -q " T _$$s$$"; then echo "MISSING export: _$$s"; missing=1; fi; \
	done; \
	if [ $$missing -ne 0 ]; then exit 1; fi; \
	echo "all $(words $(EXPORTS)) expected exports present"
	@if [ "$(STATIC_LIBUSB)" = "1" ] && otool -L $(DYLIB) | grep -q libusb; then \
	    echo "libusb is still a dynamic dependency:"; otool -L $(DYLIB); exit 1; fi
	@echo "dynamic dependencies:"; otool -L $(DYLIB) | tail -n +2

clean:
	rm -f $(DYLIB) $(SELFTEST)
	rm -rf $(DYLIB).dSYM $(SELFTEST).dSYM
