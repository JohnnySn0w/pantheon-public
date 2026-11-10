---
symptom: "ESP32-S3 freezes hard (screen dark, no serial, buttons dead) while writing an SD card, at random 10 s to 10 min in; with the interrupt watchdog on it resets with reason 5 (INT_WDT)"
fix: "serialize SD card access and gpio_wakeup_enable/disable with one application mutex (ESP-IDF 6.0.1 AB-BA spinlock deadlock between rtc_gpio_deinit and io_mux_enable_lp_io_clock)"
domain: "esp-idf / esp32-s3 / firmware"
---
## Symptom

Device hangs forever with no reset; upstream (Meta Muse SDK) ships `CONFIG_ESP_INT_WDT=n` and `CONFIG_ESP_TASK_WDT_EN=n`, so nothing recovers it. With the watchdogs on, the next boot reports `esp_reset_reason() == 5` (INT_WDT). A core dump shows two cores spinning:

```
CPU0 (SD writer):  spinlock_acquire(s_io_mux_spinlock) <- io_mux_force_disable_lp_io_clock(GPIO13)
                   <- rtc_gpio_deinit <- gpio_reset_pin <- configure_pin_gpio_matrix
                   <- sd_host_slot_set_bus_width <- sd_host_slot_sdmmc_do_transaction
CPU1 (any task):   spinlock_acquire(rtc_spinlock) <- io_mux_enable_lp_io_clock
                   <- gpio_wakeup_disable(GPIO0)
```

It only shows up when both paths run concurrently. On the Mote that meant only on battery: the button wait (`muse_gpio_buttons_wait`, which toggles `gpio_wakeup_enable`/`disable` every second) runs only while the display is paused on battery. Whatever triggers your wakeup toggling, the shape is "SD writes plus GPIO wakeup toggling on the other core".

## Cause

ESP-IDF 6.0.1 takes the same two spinlocks in opposite orders. `rtc_gpio_deinit()` takes `rtc_spinlock` (RTCIO_ENTER_CRITICAL) and then `s_io_mux_spinlock`. `io_mux_enable_lp_io_clock()` takes `s_io_mux_spinlock` and then `rtc_spinlock` (inside RTCIO_RCC_ATOMIC). The SDMMC host reaches the first path on every transaction, because it re-applies the bus width and calls `gpio_reset_pin()` on RTC-capable data pins (here GPIO 12/13). `gpio_wakeup_disable()` on an RTC-capable pin reaches the second.

## Fix

One recursive FreeRTOS mutex held around every SD card operation (mount, fopen/fwrite/fsync/fclose, stat, readdir, format) and around the `gpio_wakeup_enable`/`gpio_intr_enable` and `gpio_intr_disable`/`gpio_wakeup_disable` blocks. A mutex blocks instead of spinning, so the two paths can no longer interleave across cores. Never sleep or wait on a queue while holding it. Reference implementation: sseed-presence `735f3b2` (`muse_board_iomux_guard`: a weak no-op hook in `muse_board.c` and a real mutex in `board_waveshare_s3_185b.c`). Moving the SD writer's stack between PSRAM and internal RAM, lowering the SDMMC clock and halving PSRAM speed do NOT fix it; they only move the timing.

Related trap while diagnosing: with `CONFIG_ESP_COREDUMP_CAPTURE_DRAM=y` the dump overflows a 1 MB coredump partition ("Not enough space to save core dump!"), so the partition reads back empty and *looks* like the panic handler never ran. Capture task stacks only.

## Verification

Before the fix: the 2026-11-02 Mote battery runs logged INT_WDT resets (reason 5) every 1-2 min while recording to SD (e.g. 10 resets in 15 min, RTC boot log `>gauge.rtc`). Core dumps `battery1`/`battery2` decoded with `esp-coredump --chip esp32s3 info_corefile -t raw -c <dump> <elf>` show the two backtraces above (on CT208: /root/esp-build/muse-185b/cores/). After the fix (build 73f46cc3): 18 min on battery, recording the whole time (C00054.RAW = 39.7 MB), zero resets.

## Discovered
- T-5856 (Mote all-day capture), 2026-11-02; fix sseed-presence 735f3b2; write-up sseed-presence notes/2026-11-02-mote-all-day-capture-power-plan.md; unfiled upstream issue draft sseed-presence notes/2026-11-02-esp-idf-rtcio-iomux-deadlock-issue-draft.md

## Keywords
esp-idf, esp32-s3, sdmmc, sd card, deadlock, spinlock, rtc_spinlock, s_io_mux_spinlock, rtc_gpio_deinit, io_mux_enable_lp_io_clock, io_mux_force_disable_lp_io_clock, gpio_wakeup_disable, gpio_reset_pin, sd_host_slot_set_bus_width, interrupt watchdog, INT_WDT, reset_reason 5, InterruptWDTTimoutCPU0, hang on battery, freeze, mote, muse, waveshare 1.85, core dump empty, Not enough space to save core dump
