import { Hono } from "hono";

export type Env = {
  Bindings: {
    HL_MRF_PARSED: R2Bucket;
    HL_MRF_RAW: R2Bucket;
    ASSETS: Fetcher;
    SITE_NAME: string;
  };
};

const app = new Hono<Env>();

app.get("/", (c) => c.text("ok"));

export default app;
