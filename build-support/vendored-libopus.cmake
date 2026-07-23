# Toolchain file for building the vendored libopus (audiopus_sys) via cmake-rs.
#
# audiopus_sys's build script adds only `<out>/lib` to the linker search path,
# but GNUInstallDirs installs to `lib64` on RPM-family hosts (manylinux,
# Fedora, RHEL) — the link then fails with `unable to find library -lopus`.
# Pinning the classic layout keeps the library where the build script looks.
#
# Applied by the release workflow ONLY on native RPM-family containers.
# Do not export CMAKE_TOOLCHAIN_FILE globally: cmake-rs skips its own
# cross-compilation config when a toolchain file is present.
set(CMAKE_INSTALL_LIBDIR "lib" CACHE PATH "installation libdir pinned for audiopus_sys" FORCE)
