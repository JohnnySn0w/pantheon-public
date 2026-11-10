---
symptom: "ESP32 LVGL UI stops redrawing for good (screen won't wake on button/touch) while every other task keeps running, hours into frequent esp_lv_adapter_pause/resume cycles"
fix: "never call esp_lv_adapter_pause(-1); pause with a timeout and xTaskNotifyGive the 'lvgl' worker on timeout and after every resume"
domain: "esp-idf / esp_lvgl_adapter / lvgl / esp32-s3"
---
## Symptom

After hours of the device pausing and resuming its UI (here: display paused every time the Mote fell asleep while recording on battery), a button press logs the wake (`muse_input: waking (BOOT)`) but the screen stays dark. Audio capture, SD logging, the console and the input task all keep working; only the UI is frozen. Nothing resets it short of a reboot.

## Cause

espressif/esp_lvgl_adapter (src/adapter/esp_lv_adapter.c), `esp_lv_adapter_pause()`:

```c
s_ctx.pause_ack = false;
s_ctx.paused = true;
...
xSemaphoreTake(s_ctx.pause_done_sem, 0); /* clear pending */
xSemaphoreTake(s_ctx.pause_done_sem, ticks);
```

The worker (`lvgl_worker`) sees `paused && !pause_ack`, sets `pause_ack = true`, gives `pause_done_sem`, and parks on `ulTaskNotifyTake(pdTRUE, portMAX_DELAY)`. If it does that on the other core between `paused = true` and the "clear pending" take, the clear consumes the ack. With `timeout_ms = -1` the caller then waits forever. With a finite timeout the adapter resets `paused = false`, but the worker is still parked and `esp_lv_adapter_resume()` is a no-op when `!paused`, so nothing ever notifies it again. Either way the UI is dead. The window is a few instructions wide, so it takes thousands of pause cycles to hit.

## Fix

Own the pause wrapper (sseed-presence `44bccbd`, `display_pause()` in firmware/muse-185b/board_waveshare_s3_185b.c):

```c
static void lvgl_worker_kick(void)
{
    TaskHandle_t worker = xTaskGetHandle("lvgl");
    if (worker) xTaskNotifyGive(worker);
}

if (pause) {
    if (esp_lv_adapter_pause(500) == ESP_ERR_TIMEOUT) lvgl_worker_kick();
} else {
    esp_lv_adapter_resume();
    lvgl_worker_kick();   /* a spare notify is harmless */
}
```

The proper upstream fix is to clear `pause_done_sem` before setting `paused = true`. Fewer pause cycles (don't pause on every sleep) also shrink the exposure.

## Verification

Observed 2026-11-03 on the Mote (gated build 133b68cb): after ~2 h of battery capture the screen would not wake; a live console capture during a button press shows `talk key: press`, `waking (BOOT)`, then `sleeping (auto-sleep)` 30 s later with the panel never turning on, and `>gauge.rtc` shows the same boot recording throughout (no crash). The lost-ack interleaving is visible by reading the adapter source above. The fix's own evidence is the warning `lvgl pause lost its ack; kicking the worker and staying awake` in the console when the race is caught.

## Discovered
- T-5856 (Mote all-day capture), 2026-11-03; fix sseed-presence 44bccbd; context in sseed-presence notes/2026-11-02-mote-all-day-capture-power-plan.md

## Keywords
esp_lvgl_adapter, esp_lv_adapter_pause, esp_lv_adapter_resume, pause_done_sem, pause_ack, lvgl_worker, ulTaskNotifyTake, race, lost ack, lvgl frozen, screen won't wake, display stays dark, button wakes but no display, ui hang, esp32-s3, muse, mote, waveshare 1.85
