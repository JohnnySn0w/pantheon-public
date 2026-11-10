---
symptom: "ESP32 SPI LCD freezes for good (black or frozen screen, buttons log but nothing draws) while every other task runs; a core dump shows two tasks in spi_device_acquire_bus(portMAX_DELAY) on the same bus and nobody holding it"
fix: "never call into the SPI panel IO (esp_lcd_panel_io_tx_param, panel on/off/sleep commands) from a task other than the one streaming pixels while a queued transfer may be on the wire; funnel those calls through the sender task after its queue has drained"
domain: "esp-idf 6.0.1 / esp_lcd / spi_master / spi_bus_lock / esp32-s3"
---
## Symptom

Hours into normal use, the screen goes black (or freezes) and stays that way. The device is otherwise healthy: the console answers, audio records, a button press logs its wake, but nothing draws and the backlight never comes back. Only a reboot recovers it. On the Mote (Waveshare ESP32-S3-Touch-LCD-1.85B, ST77916 over QSPI) it happened about once per battery session on 2026-11-03.

## Cause

ESP-IDF 6.0.1 `spi_bus_lock` can lose the lock hand-over when a device acquires the bus (any polled `esp_lcd_panel_io_tx_param`, which every command and every `draw_bitmap` window setup does) while a queued DMA piece is still in flight: the device's lock bit stays set with no acquiring owner, the bus is idle, and the waiter is never woken. Every later acquirer blocks forever.

The upstream Muse firmware (`components/muse/boards/muse_lcd_bands.c`) already documents this and avoids it in its sender task with `wire_idle()`, which only calls into the panel IO when no piece is on the wire. The 1.85B board code bypassed it: `panel_sleep()` sent DISPOFF/SLPIN and SLPOUT/DISPON directly from the LVGL task (via the UI's `update_sleep()`), and could do so while `lcd_send` had a band in flight.

Core dump (watchdog abort, 2026-11-03): LVGL task in `panel_sleep(false)` -> `lcd_cmd(0x11)` -> `esp_lcd_panel_io_tx_param` -> `spi_device_acquire_bus(portMAX_DELAY)`; `lcd_send` in `esp_lcd_panel_draw_bitmap` -> `tx_param` -> `spi_device_acquire_bus` on the same lock. Decode: sseed-presence `notes/coredumps/2026-11-03-mote-ui-hang-spi-bus-lock-decode.txt`.

## Fix

Run every runtime panel-IO call on the pixel sender task, after its queue drains (sseed-presence `f7163fb`):

```c
static void panel_sleep(bool sleep)
{
    muse_lcd_bands_run(panel_sleep_job, (void *)(intptr_t)sleep);   /* wire idle first */
}
```

`muse_lcd_bands_run()` queues a call item behind any pending bands; the sender calls `wire_idle(0)` and then the function. The same applies to panel off before deep sleep.

## Verification

Before: the hang reproduced on battery about once per session (twice on 2026-11-03, once caught by the watchdog with the dump above). After the fix: no stall over a multi-hour battery day with screen wakes, checked via `>gauge.rtc` (no reset reason 4) and by waking the screen with BOOT. Status: fix flashed 2026-11-03 19:22Z; the soak is pending (T-6108).

## Diagnosing it next time

A UI-liveness watchdog catches it: an LVGL timer bumps a counter each second and an `esp_timer` aborts when it stalls 15 s while the display is not paused, which writes a flash core dump (`CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH`, tasks only) and reboots. Read it with `esptool read-flash 0xE30000 0x100000` and decode with `esp-coredump info_corefile -t raw` against the flashed ELF.

Related: [[esp-lvgl-adapter-pause-lost-ack-race-strands-ui-worker-screen-never-wakes]] (same visible symptom, different cause), [[esp-idf-sdmmc-transaction-vs-gpio-wakeup-rtcio-iomux-spinlock-deadlock-int-wdt]].
