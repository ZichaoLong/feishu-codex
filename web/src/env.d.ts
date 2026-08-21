/// <reference types="vite/client" />

// Injected by Vite `define`: an additional signal for a bundle embedded in a
// desktop shell. Runtime query/session signals remain authoritative there.
declare const __KIMI_WEB_DESKTOP__: boolean;

declare module '*.vue' {
  import type { DefineComponent } from 'vue';

  const component: DefineComponent<Record<string, never>, Record<string, never>, unknown>;
  export default component;
}

// Vite's `?worker&type=module` imports — not declared in `vite/client`,
// which only covers `?worker`, `?worker&inline`, and `?worker&url` for classic
// workers. ES module workers need this additional declaration so TypeScript
// can resolve the import without errors.
declare module '*?worker&type=module' {
  const WorkerFactory: new () => Worker;
  export default WorkerFactory;
}

// unplugin-icons `?raw` imports — `unplugin-icons/types/vue` declares
// `~icons/*` as a Vue FunctionalComponent (for direct component imports). The
// `?raw` query re-exports the raw SVG source, which must type as `string`;
// this more-specific pattern overrides the component declaration for `?raw`
// imports only (e.g. `~icons/ri/add-line?raw`), leaving component imports
// (`~icons/ri/add-line`) typed as components.
declare module '~icons/*?raw' {
  const src: string;
  export default src;
}
