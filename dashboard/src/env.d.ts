/// <reference types="vite/client" />

// Fallback for plain tsc, which can't resolve .vue SFC modules. vue-tsc (the
// repo's typecheck) resolves the real files, and TS prefers an actual module
// over this wildcard — so component types stay precise there.
declare module "*.vue" {
  import type { DefineComponent } from "vue";
  const component: DefineComponent<Record<string, unknown>>;
  export default component;
}
