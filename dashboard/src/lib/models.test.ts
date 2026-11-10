import { describe, expect, test } from "bun:test";
import { LIME_ICON, commandIcon, modelColor, modelLogo } from "./models";
import { LOGOS } from "./logos";
import { PROVIDERS } from "./providers";

describe("commandIcon", () => {
  test("uv/python/pytest/pip commands share the python icon", () => {
    const icons = [
      commandIcon("uv sync"),
      commandIcon("uv run pytest -q"),
      commandIcon("python3 -m x"),
      commandIcon("pytest -q"),
      commandIcon("pip install y"),
    ];
    const first = icons[0]!;
    for (const icon of icons) {
      expect(icon).toBe(first);
      expect(icon.startsWith("<svg")).toBe(true);
    }
  });

  test("plain shell commands get the bash icon", () => {
    const bash = commandIcon("rm -rf .venv");
    expect(bash.startsWith("<svg")).toBe(true);
    expect(bash).not.toBe(commandIcon("uv sync"));
  });

  test("no command falls back to bash", () => {
    const bash = commandIcon("rm -rf .venv");
    expect(commandIcon(null)).toBe(bash);
    expect(commandIcon(undefined)).toBe(bash);
    expect(commandIcon("")).toBe(bash);
  });

  test("matching is on the first token only", () => {
    expect(commandIcon("rm python")).toBe(commandIcon("rm -rf .venv"));
  });

  test("never the lime icon", () => {
    expect(commandIcon("uv sync")).not.toBe(LIME_ICON);
    expect(commandIcon("rm -rf .venv")).not.toBe(LIME_ICON);
  });
});

describe("modelColor", () => {
  test("stable across calls", () => {
    const c1 = modelColor("claude-fable-5-1");
    const c2 = modelColor("claude-fable-5-1");
    expect(c1).toBe(c2);
  });

  test("pairwise distinct for known models", () => {
    const ids = [
      "claude-fable-5-1",
      "sonnet",
      "openrouter/deepseek/deepseek-v4-flash",
      "xai/grok-4.6",
      "openrouter/qwen/qwen3-coder-flash",
      "openrouter/moonshotai/kimi-k3",
    ];
    const colors = ids.map(modelColor);
    expect(new Set(colors).size).toBe(ids.length);
  });

  test("every result matches /^#[0-9a-f]{6}$/", () => {
    const ids = [
      "claude-fable-5-1",
      "sonnet",
      "openrouter/deepseek/deepseek-v4-flash",
      "xai/grok-4.6",
      "openrouter/qwen/qwen3-coder-flash",
      "openrouter/moonshotai/kimi-k3",
      "some-unknown-model",
      null,
    ];
    for (const id of ids) {
      expect(modelColor(id)).toMatch(/^#[0-9a-f]{6}$/);
    }
  });

  test("unknown id ∈ PALETTE", () => {
    const color = modelColor("some-random-unknown-model");
    const palette = ["#57a639","#d5438a","#2ea3a3","#b07d00","#7a4de0","#c1451d"];
    expect(palette).toContain(color);
  });

  test("null → PALETTE[0]", () => {
    expect(modelColor(null)).toBe("#57a639");
    expect(modelColor(undefined)).toBe("#57a639");
    expect(modelColor("")).toBe("#57a639");
  });
});

describe("providers table", () => {
  test("has fifty providers with unique keys", () => {
    expect(PROVIDERS.length).toBe(50);
    const keys = PROVIDERS.map((p) => p.key);
    expect(new Set(keys).size).toBe(50);
  });

  test("every provider has a generated logo and vice versa", () => {
    const logoKeys = Object.keys(LOGOS).sort();
    const providerKeys = PROVIDERS.map((p) => p.key).sort();
    expect(logoKeys).toEqual(providerKeys);
  });

  test("every generated logo is one whole inline svg", () => {
    for (const [_key, svg] of Object.entries(LOGOS)) {
      expect(svg.startsWith("<svg")).toBe(true);
      expect(svg.endsWith("</svg>")).toBe(true);
      expect(svg).toContain("viewBox=");
      expect(svg).not.toContain("<title");
      expect(svg).not.toContain("lobe-icons-");
      expect(svg).not.toBe(LIME_ICON);
    }
  });

  test("needles are safe substrings", () => {
    for (const p of PROVIDERS) {
      for (const n of p.needles) {
        expect(n).toBe(n.toLowerCase());
        expect(n.length).toBeGreaterThanOrEqual(3);
        expect(n).not.toBe("openrouter");
        expect(n).not.toContain("openrouter");
      }
    }
  });
});

describe("modelLogo", () => {
  test("unknown or empty ids fall back to the lime asterisk", () => {
    expect(modelLogo("some-unknown-model")).toBe(LIME_ICON);
    expect(modelLogo("openrouter/nobody/model-x")).toBe(LIME_ICON);
    expect(modelLogo(null)).toBe(LIME_ICON);
    expect(modelLogo(undefined)).toBe(LIME_ICON);
    expect(modelLogo("")).toBe(LIME_ICON);
  });

  test("ids seen in lime.db resolve to their provider", () => {
    const cases: [string, string][] = [
      ["claude-fable-5-1", "claude"],
      ["claude-opus-5[1m]", "claude"],
      ["fable", "claude"],
      ["sonnet", "claude"],
      ["haiku", "claude"],
      ["openrouter/deepseek/deepseek-v4-flash", "deepseek"],
      ["openrouter/qwen/qwen3-coder-flash", "qwen"],
      ["openrouter/google/gemini-3.8-flash", "gemini"],
      ["xai/grok-4.6", "grok"],
      ["openrouter/moonshotai/kimi-k3", "kimi"],
      ["openrouter/z-ai/glm-5.3-flash", "glm"],
      ["openrouter/meta/muse-spark-1.3", "meta"],
    ];
    for (const [id, key] of cases) {
      expect(modelLogo(id)).toBe(LOGOS[key]);
    }
  });

  test("one representative id per provider resolves to exactly that provider's mark", () => {
    const cases: [string, string][] = [
      ["openrouter/anthropic/claude-sonnet-5", "claude"],
      ["deepseek-r1-distill-llama-70b", "deepseek"],
      ["openrouter/alibaba/qwq-32b", "qwen"],
      ["openrouter/google/gemma-3-27b", "gemini"],
      ["x-ai/grok-4", "grok"],
      ["groq/llama-3.3-70b", "groq"],
      ["together/meta-llama/llama-3-70b", "together"],
      ["fireworks/llama-v3p1-405b", "fireworks"],
      ["deepinfra/meta-llama/llama-3", "deepinfra"],
      ["cerebras/llama3.1-8b", "cerebras"],
      ["sambanova/meta-llama-3.1-405b", "sambanova"],
      ["moonshotai/kimi-k2", "kimi"],
      ["openrouter/meta-llama/llama-4-maverick", "meta"],
      ["openrouter/zhipu/chatglm-4", "glm"],
      ["openrouter/openai/gpt-5", "openai"],
      ["openrouter/mistralai/devstral-medium", "mistral"],
      ["openrouter/microsoft/phi-4", "microsoft"],
      ["openrouter/nvidia/nemotron-ultra", "nvidia"],
      ["openrouter/cohere/command-a", "cohere"],
      ["openrouter/perplexity/sonar-pro", "perplexity"],
      ["openrouter/ai21/jamba-large", "ai21"],
      ["openrouter/amazon/nova-pro-v1", "amazon"],
      ["openrouter/ibm-granite/granite-4", "ibm"],
      ["openrouter/nousresearch/hermes-4", "nous"],
      ["openrouter/liquid/lfm-7b", "liquid"],
      ["openrouter/tencent/hunyuan-a13b", "tencent"],
      ["openrouter/bytedance/seed-oss-36b", "bytedance"],
      ["openrouter/baidu/ernie-4.5", "baidu"],
      ["openrouter/xiaomi/mimo-v2-flash", "xiaomi"],
      ["openrouter/01-ai/yi-large", "yi"],
      ["openrouter/minimax/minimax-m2", "minimax"],
      ["openrouter/stepfun/step-3", "stepfun"],
      ["openrouter/snowflake/arctic-instruct", "snowflake"],
      ["openrouter/databricks/dbrx-instruct", "databricks"],
      ["openrouter/upstage/solar-pro", "upstage"],
      ["openrouter/internlm/intern-s1", "internlm"],
      ["openrouter/baichuan/baichuan-m2", "baichuan"],
      ["openrouter/huawei/pangu-pro", "huawei"],
      ["venice/venice-uncensored", "venice"],
      ["featherless/featherless-model", "featherless"],
      ["openrouter/inception/mercury-coder", "inception"],
      ["nebius/nebius-fast", "nebius"],
      ["openrouter/arcee-ai/coder-large", "arcee"],
      ["openrouter/allenai/olmo-3", "allenai"],
      ["openrouter/inflection/inflection-3-pi", "inflection"],
      ["openrouter/morph/morph-v3-large", "morph"],
      ["openrouter/deepcogito/cogito-v2", "cogito"],
      ["openrouter/kwaipilot/kat-coder", "kwaipilot"],
      ["openrouter/skywork/skywork-r1v", "skywork"],
      ["openrouter/tiiuae/falcon-3", "tii"],
    ];
    for (const [id, key] of cases) {
      expect(modelLogo(id)).toBe(LOGOS[key]);
    }
  });

  test("families still resolve without a provider slug", () => {
    expect(modelLogo("llama-3.3-70b")).toBe(LOGOS.meta);
    expect(modelLogo("mixtral-8x7b")).toBe(LOGOS.mistral);
    expect(modelLogo("phi-4")).toBe(LOGOS.microsoft);
    expect(modelLogo("nemotron-70b")).toBe(LOGOS.nvidia);
    expect(modelLogo("gpt-5")).toBe(LOGOS.openai);
    expect(modelLogo("glm-5.3-flash")).toBe(LOGOS.glm);
    expect(modelLogo("gemma-3")).toBe(LOGOS.gemini);
  });

  test("neighbours do not cross-match", () => {
    expect(modelLogo("xai/grok-4.6")).toBe(LOGOS.grok);
    expect(modelLogo("xai/grok-4.6")).not.toBe(LOGOS.groq);
    expect(modelLogo("groq/llama-3.3-70b")).not.toBe(LOGOS.grok);
    expect(modelLogo("openrouter/xiaomi/mimo-v2-flash")).toBe(LOGOS.xiaomi);
    expect(modelLogo("openrouter/minimax/minimax-m2")).toBe(LOGOS.minimax);
  });

  test("modelColor still keys off the same table", () => {
    expect(modelColor("openrouter/deepseek/deepseek-v4-flash")).toBe("#4d6bfe");
    expect(modelColor("xai/grok-4.6")).toBe("#e8940a");
    expect(modelColor("openrouter/meta/muse-spark-1.3")).toMatch(/^#[0-9a-f]{6}$/);
  });
});