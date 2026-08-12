// ============================================================
// Knowledge Pro Dashboard — Config
// Uses the PUBLISHABLE/ANON key only. Never put the secret key here.
// ============================================================
const SUPABASE_URL = "http://ncjstamtjdmypfmhyabq.supabase.co";
const SUPABASE_ANON_KEY = "sb_secret_hIN-cScWrlXkSg1czBhV4w_iF4MD62G";

if (!window.supabase) {
  console.error("Supabase JS library failed to load from CDN. Check your network/ad-blocker and that the <script> tag for @supabase/supabase-js loads BEFORE config.js.");
}
window.supabaseClient = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
const supabaseClient = window.supabaseClient;