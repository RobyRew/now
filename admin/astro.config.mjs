// @ts-check
import { createHash } from 'node:crypto';
import { defineConfig } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';
import { houseDefaults } from '@robyrew/web-config/astro';
import { themeBootstrap } from '@robyrew/web-config/theme-bootstrap';

// The admin page is static: built here, served by the Python service under /admin/, and it talks to the service's
// /admin/api/*. The service adds the headers a meta tag cannot carry (frame-ancestors, HSTS).

// The pre-paint theme stamp is an inline script; its CSP hash is computed from the package's own string.
/** @type {`sha256-${string}`} */
const bootstrapHash = `sha256-${createHash('sha256').update(themeBootstrap).digest('base64')}`;

export default defineConfig({
  ...houseDefaults,
  site: process.env.PUBLIC_SITE_URL || 'https://now.spotify.robyrew.com',
  base: '/admin',
  output: 'static',
  build: { ...houseDefaults.build, inlineStylesheets: 'never' },
  security: {
    csp: {
      directives: [
        "default-src 'self'",
        // Covers come from Spotify's image CDN.
        "img-src 'self' https://i.scdn.co data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
      ],
      scriptDirective: { hashes: [bootstrapHash] },
    },
  },
  server: { port: 4352 },
  markdown: { syntaxHighlight: false },
  vite: {
    plugins: [tailwindcss()],
    build: { target: 'safari16', cssTarget: 'safari16' },
  },
});
