// ============================================================
// Knowledge Pro Dashboard — Config
// Uses the PUBLISHABLE/ANON key only. Never put the secret key here.
// ============================================================
const SUPABASE_URL = "https://ncjstamtjdmypfmhyabq.supabase.co";
const SUPABASE_ANON_KEY = "sb_publishable_dz9aXffOP_hApwiPY5123Q_GQu8dqIP";

if (!window.supabase) {
  console.error("Supabase JS library failed to load from CDN. Check your network/ad-blocker and that the <script> tag for @supabase/supabase-js loads BEFORE config.js.");
}
window.supabaseClient = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
const supabaseClient = window.supabaseClient;
