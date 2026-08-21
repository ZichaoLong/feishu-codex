import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import Icons from 'unplugin-icons/vite';
import { FileSystemIconLoader } from 'unplugin-icons/loaders';
import { fileURLToPath } from 'node:url';
import { focusThirdPartyNoticesPlugin } from './scripts/vite-third-party-notices.mjs';
import { focusBundleBoundaryPlugin } from './scripts/focus-bundle-boundary';

const webPort = Number(process.env.WEB_PORT) || 5175;
const gatewayTarget = process.env.FOCUS_WEB_GATEWAY_URL || 'http://127.0.0.1:8766';

const apiProxyOptions = {
  target: gatewayTarget,
  changeOrigin: true,
  ws: true,
  configure(proxy: {
    on(event: string, listener: (proxyRequest: {
      setHeader(name: string, value: string): void;
    }) => void): unknown;
  }) {
    const rewriteOrigin = (proxyRequest: { setHeader(name: string, value: string): void }): void => {
      proxyRequest.setHeader('origin', gatewayTarget);
    };
    proxy.on('proxyReq', rewriteOrigin);
    proxy.on('proxyReqWs', rewriteOrigin);
  },
};

export default defineConfig({
  plugins: [
    vue(),
    focusBundleBoundaryPlugin(),
    Icons({
      compiler: 'vue3',
      // Keep the existing internal collection name so current icon imports stay
      // stable. It records asset ancestry; it is not Kimi product alignment or
      // Focus branding.
      customCollections: {
        kimi: FileSystemIconLoader(fileURLToPath(new URL('./src/icons/kimi', import.meta.url))),
      },
    }),
    focusThirdPartyNoticesPlugin(),
  ],
  define: {
    __KIMI_WEB_DESKTOP__: JSON.stringify(false),
  },
  server: {
    port: webPort,
    strictPort: false,
    proxy: { '/api': apiProxyOptions },
  },
  preview: {
    port: Number(process.env.WEB_PREVIEW_PORT) || 4175,
    proxy: { '/api': apiProxyOptions },
  },
  build: {
    outDir: '../bot/web_assets/dist',
    emptyOutDir: true,
    target: 'es2022',
  },
  worker: { format: 'es' },
});
