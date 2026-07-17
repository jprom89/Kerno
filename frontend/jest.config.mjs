/**
 * jest.config.mjs — frontend test runner configuration (KER-301 AC-8).
 *
 * What:  wires Jest through next/jest so tests compile with the same SWC
 *        config as the app; default environment is node (route handlers and
 *        middleware run server-side), individual files opt into jsdom with a
 *        docblock pragma.
 * Why:   the auth flows under test are server code — cookies, redirects —
 *        not browser code; jsdom is only needed where React renders.
 * How:   npm test
 */

import nextJest from "next/jest.js";

const createJestConfig = nextJest({ dir: "./" });

const config = {
  testEnvironment: "node",
  testMatch: ["**/__tests__/**/*.test.{ts,tsx}"],
  clearMocks: true,
};

export default createJestConfig(config);
