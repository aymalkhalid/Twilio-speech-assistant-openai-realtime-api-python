-- Call-record storage schema for the generic voice-agent starter.
-- Run in the Supabase SQL Editor when enabling CALL_RECORD_BACKEND=supabase.
--
-- Default app setting:
--   SUPABASE_CALL_RECORD_TABLE=call_records
--
-- Note: v1 keeps a few lead_* column names as storage-compatibility fields
-- behind services/call_records_service.py. The app, prompt, tools, and APIs use
-- call-record language.

CREATE TABLE IF NOT EXISTS public.call_records (
  id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at                timestamptz NOT NULL DEFAULT now(),

  -- Call identity
  call_sid                  text UNIQUE,
  "timestamp"               timestamptz,

  -- Contact
  lead_name                 text,
  lead_email                text,
  lead_phone                text,

  -- Call content
  company_name              text,
  agent_label               text,
  industry                  text,
  priority                  text,
  issue_summary             text,
  call_summary              text,
  preferred_callback_time   text,
  service_address           text,

  -- Booking
  confirmed_slot            jsonb,
  calendar_event_link       text,

  -- Recording and transcript
  recording_link            text,
  transcript                text,
  transcript_enhanced_at    timestamptz,
  transcript_summary        text,
  transcript_issues         text,

  -- Dashboard workflow
  notes                     jsonb DEFAULT '[]'::jsonb,
  lead_status               text DEFAULT 'pending',

  -- Extra
  metadata                  jsonb
);

COMMENT ON TABLE public.call_records IS 'Call records captured by the generic voice agent; one row per call_sid.';
COMMENT ON COLUMN public.call_records.call_sid IS 'Twilio call SID; unique per call and used for updates.';
COMMENT ON COLUMN public.call_records.lead_name IS 'Compatibility column for contact name.';
COMMENT ON COLUMN public.call_records.lead_email IS 'Compatibility column for contact email.';
COMMENT ON COLUMN public.call_records.lead_phone IS 'Compatibility column for contact phone.';
COMMENT ON COLUMN public.call_records.service_address IS 'Compatibility column for location/address when relevant.';
COMMENT ON COLUMN public.call_records.lead_status IS 'Compatibility column for workflow status: pending, in_progress, booked, completed, finalized, spam.';
COMMENT ON COLUMN public.call_records.confirmed_slot IS 'Booked slot returned by book_appointment.';
COMMENT ON COLUMN public.call_records.transcript_enhanced_at IS 'Set when transcript is AI-enhanced from the dashboard.';

CREATE INDEX IF NOT EXISTS idx_call_records_created_at ON public.call_records (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_call_records_status ON public.call_records (lead_status);
CREATE INDEX IF NOT EXISTS idx_call_records_call_sid ON public.call_records (call_sid);
