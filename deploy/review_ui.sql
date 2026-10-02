BEGIN;
CREATE OR REPLACE FUNCTION public.tm_review_submission_v20(p_instance_id text,p_request_key text)
RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path='' AS $$
DECLARE saved jsonb;
BEGIN
 IF p_instance_id IS DISTINCT FROM 'macbook-owner' OR length(p_request_key) NOT BETWEEN 16 AND 128 THEN
  RAISE EXCEPTION 'invalid_review_receipt';
 END IF;
 SELECT result INTO saved FROM tm_api_private.operations
 WHERE instance_id=p_instance_id AND request_key=p_request_key AND operation='answer_review_question';
 RETURN jsonb_build_object('committed',FOUND,'result',saved);
END $$;
REVOKE ALL ON FUNCTION public.tm_review_submission_v20(text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.tm_review_submission_v20(text,text) TO service_role;
CREATE OR REPLACE FUNCTION public.tm_personal_contract_v18(p_instance_id text)
RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path='' AS $fn$
DECLARE profile jsonb; settings jsonb; sequential boolean;
BEGIN
 IF p_instance_id IS DISTINCT FROM 'macbook-owner' OR NOT public.tm_instance_known_v18(p_instance_id) THEN
  RAISE EXCEPTION 'wrong_personal_installation';
 END IF;
 profile:=public.tm_review_display_profile_v15(NULL);
 settings:=profile->'settings';
 sequential:=settings->>'quick_answer_style'='immediate_single_question';
 IF profile IS NULL OR settings->>'layout' IS DISTINCT FROM 'cards' OR
    (coalesce(sequential,false) AND (settings->'quick_answer_footer' IS DISTINCT FROM 'false'::jsonb OR
      settings->'show_copy_answer' IS DISTINCT FROM 'false'::jsonb OR settings->'show_reset_selection' IS DISTINCT FROM 'false'::jsonb OR
      settings->>'selected_button_style' IS DISTINCT FROM 'solid_blue' OR settings->'quick_answer_enabled' IS DISTINCT FROM 'true'::jsonb OR
      settings->'always_include_clarify' IS DISTINCT FROM 'true'::jsonb OR settings->'no_fake_buttons' IS DISTINCT FROM 'true'::jsonb)) OR
    (NOT coalesce(sequential,false) AND settings->'quick_answer_footer' IS DISTINCT FROM 'true'::jsonb)
 THEN RAISE EXCEPTION 'saved_review_profile_missing'; END IF;
 RETURN jsonb_build_object('client',18,'core',16,'mode','personal_v16_upgrade','instance_id',p_instance_id,
   'review_profile_key',profile->>'profile_key','review_profile_version',profile->'version',
   'operating_contract',public.tm_operating_contract_v18(),'error_history',true,'focus_persistent',true);
END $fn$;
DO $$
DECLARE profile public.tm_review_display_profiles_v15%rowtype; settings jsonb;
BEGIN
 SELECT * INTO STRICT profile FROM public.tm_review_display_profiles_v15 WHERE is_default;
 settings:=profile.settings-ARRAY['quick_answer_title','quick_answer_placeholder','copy_answer_label','reset_selection_label'];
 settings:=settings||'{"quick_answer_footer":false,"show_copy_answer":false,"show_reset_selection":false,"quick_answer_style":"immediate_single_question","selected_button_style":"solid_blue"}'::jsonb;
 PERFORM public.tm_set_review_display_profile_v15(profile.profile_key,settings,true,profile.version);
END $$;
NOTIFY pgrst,'reload schema';
COMMIT;
