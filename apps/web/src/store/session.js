// src/store/session.js
import { signal } from "@preact/signals";

export const session = signal({
  authenticated: false,
  username: null,
  auth_required: true,
  password_login_enabled: true,
  users_configured: false,
  checked: false,
});
