// src/store/run.js
// Live run + overview state, polled every 4s by the Register page.
// regFormDirty guards form edits from being wiped by background config reloads.
import { signal } from "@preact/signals";

export const currentRunState = signal(null); // run object | null
export const overviewState = signal(null);
export const regFormDirty = signal(false); // true → poll must not overwrite form fields
export const regFormLoaded = signal(false); // first successful config load happened?

// last known disk product_ok (survives transient overview fetch failures)
export const lastProductOk = signal(null);
