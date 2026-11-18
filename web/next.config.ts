import type { NextConfig } from "next";

const config: NextConfig = {
  output: "standalone", // self-contained server for the container image
  poweredByHeader: false,
  reactStrictMode: true,
};

export default config;
