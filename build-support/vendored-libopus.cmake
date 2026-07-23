# Toolchain file for building the vendored libopus (audiopus_sys) via cmake-rs.
#
# audiopus_sys's build script adds only `<out>/lib` to the linker search path,
# but GNUInstallDirs installs to `lib64` on RPM-family hosts (manylinux,
# Fedora, RHEL) — the link then fails with `unable to find library -lopus`.
# Pinning the classic layout keeps the library where the build script looks.
# Referenced relatively from .cargo/config.toml (ships in the sdist).
set(CMAKE_INSTALL_LIBDIR "lib" CACHE PATH "installation libdir pinned for audiopus_sys" FORCE)
