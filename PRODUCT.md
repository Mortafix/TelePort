# TelePort

<!-- impeccable:product-schema 1 -->

## Platform
web

## Users
One owner and administrator of one Telegram account. Guests access individual conversations using their password.

## Product Purpose
Explore a private Telegram archive through accurate statistics, conversation moments, original messages, and contextual sentiment.

## Operating Context
Self-hosted on the owner's VPS. Existing CSV exports must remain intact and importable. Reconnection, importing selected dialogs and incremental updates are admin operations.

## Capabilities and Constraints
React with Vite, FastAPI and Python background processing. Preserve every legacy report statistic, correct chronology and calculations, and add useful measures of conversational rhythm. OpenAI sentiment analyzes time-based blocks, preserves speaker context and only reprocesses changed content. Admin can reveal chat passwords and bypass individual chat locks during the authenticated session. Explicit demo mode uses synthetic content; live operation never silently substitutes demo content.

## Brand Commitments
Keep the TelePort name. User selected Atlante from output/ui-concepts/atlante.png and its matching admin concept.

## Product Principles
- Keep original messages available as evidence behind aggregate statistics.
- Display import and analysis progress independently.
- Protect private content and keep costs visible and bounded.
- Distinguish uncertainty from neutral or mixed sentiment.
