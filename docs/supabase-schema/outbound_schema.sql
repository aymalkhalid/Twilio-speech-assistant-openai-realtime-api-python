-- Outbound campaigns: tables for outbound calling feature.
-- Run in the Supabase SQL Editor after call_records_schema.sql when OUTBOUND_ENABLED=true.
-- Safe to re-run: uses CREATE TABLE IF NOT EXISTS.

-- =============================================================================
-- 1. OUTBOUND CAMPAIGNS TABLE
-- =============================================================================
-- One row per campaign; created from dashboard, updated as campaign progresses.

CREATE TABLE IF NOT EXISTS public.outbound_campaigns (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at        timestamptz NOT NULL DEFAULT now(),

  -- Campaign identity
  name              text NOT NULL,
  campaign_type     text NOT NULL DEFAULT 'general',
  message_template  text,
  concurrency       int NOT NULL DEFAULT 1,

  -- Lifecycle
  status            text NOT NULL DEFAULT 'draft',
  started_at        timestamptz,
  completed_at      timestamptz,

  -- Extra
  metadata          jsonb
);

COMMENT ON TABLE  public.outbound_campaigns IS 'Dashboard-created outbound calling campaigns. Status: draft → running → paused → completed.';
COMMENT ON COLUMN public.outbound_campaigns.campaign_type IS 'promo | appointment_confirmation | payment_reminder | general';
COMMENT ON COLUMN public.outbound_campaigns.message_template IS 'AI system prompt / script template with {contact_name}, {company_name}, etc.';
COMMENT ON COLUMN public.outbound_campaigns.concurrency IS 'Max simultaneous outbound calls for this campaign.';
COMMENT ON COLUMN public.outbound_campaigns.status IS 'draft | running | paused | completed';

CREATE INDEX IF NOT EXISTS idx_outbound_campaigns_status     ON public.outbound_campaigns (status);
CREATE INDEX IF NOT EXISTS idx_outbound_campaigns_created_at ON public.outbound_campaigns (created_at DESC);

-- =============================================================================
-- 2. OUTBOUND CONTACTS TABLE
-- =============================================================================
-- One row per contact in a campaign; updated as each call progresses.

CREATE TABLE IF NOT EXISTS public.outbound_contacts (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id     uuid NOT NULL REFERENCES public.outbound_campaigns(id) ON DELETE CASCADE,

  -- Contact info
  name            text,
  phone           text NOT NULL,
  email           text,
  custom_fields   jsonb DEFAULT '{}'::jsonb,

  -- Call lifecycle
  status          text NOT NULL DEFAULT 'pending',
  call_sid        text,
  error           text,
  called_at       timestamptz,
  completed_at    timestamptz
);

COMMENT ON TABLE  public.outbound_contacts IS 'Contacts belonging to an outbound campaign. Status: pending → calling → completed | failed | skipped.';
COMMENT ON COLUMN public.outbound_contacts.custom_fields IS 'Campaign-type-specific fields: e.g. {"appointment_date": "...", "amount_due": "..."}';
COMMENT ON COLUMN public.outbound_contacts.call_sid IS 'Twilio Call SID once the outbound call is initiated.';

CREATE INDEX IF NOT EXISTS idx_outbound_contacts_campaign_id ON public.outbound_contacts (campaign_id);
CREATE INDEX IF NOT EXISTS idx_outbound_contacts_status      ON public.outbound_contacts (status);
