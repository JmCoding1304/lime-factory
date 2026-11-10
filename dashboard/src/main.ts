import { createApp } from "vue";
import "@fontsource/space-grotesk/300.css";
import "@fontsource/space-grotesk/400.css";
import "@fontsource/space-grotesk/500.css";
import "@fontsource/space-grotesk/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import App from "./App.vue";
import "./style.css";
import { installKeymap } from "./lib/keymap";

installKeymap();
createApp(App).mount("#app");
