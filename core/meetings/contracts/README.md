# contracts — published by `meetings`

Language-neutral contracts this domain owns (transcript · lifecycle · acts · invocation ·
service-authority). Consumers reference them across the boundary (the legitimate seam); a domain
may depend on another domain's `contracts/` but never its `services/`/`modules/`. `gate:schema`
validates goldens ≡ schema.

`runtime-callback/` is not a sealed contract: it holds the shared test vectors for the per-bot token
in the runtime callbackUrl (design §1.10).
