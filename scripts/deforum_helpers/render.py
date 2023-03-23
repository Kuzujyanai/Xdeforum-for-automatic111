import os
import json
import pandas as pd
import cv2
import re
import numpy as np
import itertools
import numexpr
import gc
import random
import PIL
from PIL import Image, ImageOps
from .rich import console
from .generate import generate, isJson
from .noise import add_noise
from .animation import sample_from_cv2, sample_to_cv2, anim_frame_warp
from .animation_key_frames import DeformAnimKeys, LooperAnimKeys
from .video_audio_utilities import get_frame_name, get_next_frame
from .depth import MidasModel, AdaBinsModel
from .colors import maintain_colors
from .parseq_adapter import ParseqAnimKeys
from .seed import next_seed
from .blank_frame_reroll import blank_frame_reroll
from .image_sharpening import unsharp_mask
from .load_images import get_mask, load_img, load_image, get_mask_from_file
from .hybrid_video import (
    hybrid_generation, hybrid_composite, get_matrix_for_hybrid_motion, get_matrix_for_hybrid_motion_prev, get_flow_for_hybrid_motion,get_flow_for_hybrid_motion_prev,
    image_transform_ransac, image_transform_optical_flow, get_flow_from_images, abs_flow_to_rel_flow, rel_flow_to_abs_flow)
from .save_images import save_image
from .composable_masks import compose_mask_with_check
from .settings import save_settings_from_animation_run
from .deforum_controlnet import unpack_controlnet_vids, is_controlnet_enabled
# Webui
from modules.shared import opts, cmd_opts, state, sd_model
from modules import lowvram, devices, sd_hijack

def render_animation(args, anim_args, video_args, parseq_args, loop_args, controlnet_args, animation_prompts, root):
    # handle hybrid video generation
    if anim_args.animation_mode in ['2D','3D']:
        if anim_args.hybrid_composite or anim_args.hybrid_motion in ['Affine', 'Perspective', 'Optical Flow']:
            args, anim_args, inputfiles = hybrid_generation(args, anim_args, root)
            # path required by hybrid functions, even if hybrid_comp_save_extra_frames is False
            hybrid_frame_path = os.path.join(args.outdir, 'hybridframes')
        # initialize prev_flow
        if anim_args.hybrid_motion == 'Optical Flow':
            prev_flow = None

        if loop_args.use_looper:
            print("Using Guided Images mode: seed_behavior will be set to 'schedule' and 'strength_0_no_init' to False")
            if args.strength == 0:
                raise RuntimeError("Strength needs to be greater than 0 in Init tab")
            args.strength_0_no_init = False
            args.seed_behavior = "schedule"
            if not isJson(loop_args.init_images):
                raise RuntimeError("The images set for use with keyframe-guidance are not in a proper JSON format")

    # handle controlnet video input frames generation
    if is_controlnet_enabled(controlnet_args):
        unpack_controlnet_vids(args, anim_args, video_args, parseq_args, loop_args, controlnet_args, animation_prompts, root)

    # use parseq if manifest is provided
    use_parseq = parseq_args.parseq_manifest != None and parseq_args.parseq_manifest.strip()
    # expand key frame strings to values
    keys = DeformAnimKeys(anim_args, args.seed) if not use_parseq else ParseqAnimKeys(parseq_args, anim_args, video_args)
    loopSchedulesAndData = LooperAnimKeys(loop_args, anim_args, args.seed)
    # resume animation
    start_frame = 0
    if anim_args.resume_from_timestring:
        for tmp in os.listdir(args.outdir):
            if ".txt" in tmp : 
                pass
            else:
                filename = tmp.split("_")
                # don't use saved depth maps to count number of frames
                if anim_args.resume_timestring in filename and "depth" not in filename:
                    start_frame += 1
        #start_frame = start_frame - 1

    # create output folder for the batch
    os.makedirs(args.outdir, exist_ok=True)
    print(f"Saving animation frames to:\n{args.outdir}")
    
    # save settings.txt file for the current run
    save_settings_from_animation_run(args, anim_args, parseq_args, loop_args, controlnet_args, video_args, root)

    # resume from timestring
    if anim_args.resume_from_timestring:
        args.timestring = anim_args.resume_timestring

    # Always enable pseudo-3d with parseq. No need for an extra toggle:
    # Whether it's used or not in practice is defined by the schedules
    if use_parseq:
        anim_args.flip_2d_perspective = True        

    # expand prompts out to per-frame
    if use_parseq and keys.manages_prompts():
        prompt_series = keys.prompts
    else:
        prompt_series = pd.Series([np.nan for a in range(anim_args.max_frames)])
        max_f = anim_args.max_frames - 1
        for i, prompt in animation_prompts.items():
            if str(i).isdigit():
                prompt_series[int(i)] = prompt
            else:
                prompt_series[int(numexpr.evaluate(i))] = prompt
        prompt_series = prompt_series.ffill().bfill()

    # check for video inits
    using_vid_init = anim_args.animation_mode == 'Video Input'

    # load depth model for 3D
    predict_depths = (anim_args.animation_mode == '3D' and anim_args.use_depth_warping) or anim_args.save_depth_maps
    predict_depths = predict_depths or (anim_args.hybrid_composite and anim_args.hybrid_comp_mask_type in ['Depth','Video Depth'])
    if predict_depths:
        keep_in_vram = opts.data.get("deforum_keep_3d_models_in_vram")
        
        device = ('cpu' if cmd_opts.lowvram or cmd_opts.medvram else root.device)
        depth_model = MidasModel(root.models_path, device, root.half_precision, keep_in_vram=keep_in_vram)
        
        if anim_args.midas_weight < 1.0:
            adabins_model = AdaBinsModel(root.models_path, keep_in_vram=keep_in_vram)
            
        # depth-based hybrid composite mask requires saved depth maps
        if anim_args.hybrid_composite and anim_args.hybrid_comp_mask_type =='Depth':
            anim_args.save_depth_maps = True
    else:
        depth_model = None
        anim_args.save_depth_maps = False

    # state for interpolating between diffusion steps
    turbo_steps = 1 if using_vid_init else int(anim_args.diffusion_cadence)
    turbo_prev_image, turbo_prev_frame_idx = None, 0
    turbo_next_image, turbo_next_frame_idx = None, 0

    # resume animation
    prev_img = None
    color_match_sample = None
    if anim_args.resume_from_timestring:
        last_frame = start_frame-1
        if turbo_steps > 1:
            last_frame -= last_frame%turbo_steps
        path = os.path.join(args.outdir,f"{args.timestring}_{last_frame:09}.png")
        img = cv2.imread(path)
        prev_img = img
        if anim_args.color_coherence != 'None':
            color_match_sample = img
        if turbo_steps > 1:
            turbo_next_image, turbo_next_frame_idx = prev_img, last_frame
            turbo_prev_image, turbo_prev_frame_idx = turbo_next_image, turbo_next_frame_idx
            start_frame = last_frame+turbo_steps

    args.n_samples = 1
    frame_idx = start_frame

    # reset the mask vals as they are overwritten in the compose_mask algorithm
    mask_vals = {}
    noise_mask_vals = {}

    mask_vals['everywhere'] = Image.new('1', (args.W, args.H), 1)
    noise_mask_vals['everywhere'] = Image.new('1', (args.W, args.H), 1)

    mask_image = None
    
    if args.use_init and args.init_image != None and args.init_image != '':
        _, mask_image = load_img(args.init_image, 
                                        shape=(args.W, args.H),  
                                        use_alpha_as_mask=args.use_alpha_as_mask)
        mask_vals['video_mask'] = mask_image
        noise_mask_vals['video_mask'] = mask_image
    
    # Grab the first frame masks since they wont be provided until next frame    
    # Video mask overrides the init image mask, also, won't be searching for init_mask if use_mask_video is set
    # Made to solve https://github.com/deforum-art/deforum-for-automatic1111-webui/issues/386
    if anim_args.use_mask_video:

        args.mask_file = get_mask_from_file(get_next_frame(args.outdir, anim_args.video_mask_path, frame_idx, True), args)
        args.noise_mask = get_mask_from_file(get_next_frame(args.outdir, anim_args.video_mask_path, frame_idx, True), args)

        mask_vals['video_mask'] = get_mask_from_file(get_next_frame(args.outdir, anim_args.video_mask_path, frame_idx, True), args)
        noise_mask_vals['video_mask'] = get_mask_from_file(get_next_frame(args.outdir, anim_args.video_mask_path, frame_idx, True), args)
    elif mask_image is None and args.use_mask:
        mask_vals['video_mask'] = get_mask(args)
        noise_mask_vals['video_mask'] = get_mask(args) # TODO?: add a different default noisc mask

    # get color match for 'Image' color coherence only once, before loop
    if anim_args.color_coherence == 'Image':
        color_match_sample = load_image(anim_args.color_coherence_image_path)
        color_match_sample = color_match_sample.resize((args.W, args.H), PIL.Image.LANCZOS)
        color_match_sample = cv2.cvtColor(np.array(color_match_sample), cv2.COLOR_RGB2BGR)

    # initialize cv2 border mode based on args
    cv2_border_mode = cv2.BORDER_WRAP if anim_args.border == 'wrap' else cv2.BORDER_REPLICATE

    #Webui
    state.job_count = anim_args.max_frames

    while frame_idx < (anim_args.max_frames if not anim_args.use_mask_video else anim_args.max_frames - 1):
        #Webui
        state.job = f"frame {frame_idx + 1}/{anim_args.max_frames}"
        state.job_no = frame_idx + 1
        if state.interrupted:
            break

        print(f"\033[36mAnimation frame: \033[0m{frame_idx}/{anim_args.max_frames}  ")

        noise = keys.noise_schedule_series[frame_idx]
        strength = keys.strength_schedule_series[frame_idx]
        scale = keys.cfg_scale_schedule_series[frame_idx]
        contrast = keys.contrast_schedule_series[frame_idx]
        kernel = int(keys.kernel_schedule_series[frame_idx])
        sigma = keys.sigma_schedule_series[frame_idx]
        amount = keys.amount_schedule_series[frame_idx]
        threshold = keys.threshold_schedule_series[frame_idx]
        hybrid_comp_schedules = {
            "alpha": keys.hybrid_comp_alpha_schedule_series[frame_idx],
            "mask_blend_alpha": keys.hybrid_comp_mask_blend_alpha_schedule_series[frame_idx],
            "mask_contrast": keys.hybrid_comp_mask_contrast_schedule_series[frame_idx],
            "mask_auto_contrast_cutoff_low": int(keys.hybrid_comp_mask_auto_contrast_cutoff_low_schedule_series[frame_idx]),
            "mask_auto_contrast_cutoff_high": int(keys.hybrid_comp_mask_auto_contrast_cutoff_high_schedule_series[frame_idx]),
            "flow_factor": int(keys.hybrid_flow_factor_schedule_series[frame_idx])
        }        
        scheduled_sampler_name = None
        scheduled_clipskip = None
        scheduled_noise_multiplier = None
        mask_seq = None
        noise_mask_seq = None
        if anim_args.enable_steps_scheduling and keys.steps_schedule_series[frame_idx] is not None:
            args.steps = int(keys.steps_schedule_series[frame_idx])
        if anim_args.enable_sampler_scheduling and keys.sampler_schedule_series[frame_idx] is not None:
            scheduled_sampler_name = keys.sampler_schedule_series[frame_idx].casefold()
        if anim_args.enable_clipskip_scheduling and keys.clipskip_schedule_series[frame_idx] is not None:
            scheduled_clipskip = int(keys.clipskip_schedule_series[frame_idx])
        if anim_args.enable_noise_multiplier_scheduling and keys.noise_multiplier_schedule_series[frame_idx] is not None:
            scheduled_noise_multiplier = float(keys.noise_multiplier_schedule_series[frame_idx])
        if args.use_mask and keys.mask_schedule_series[frame_idx] is not None:
            mask_seq = keys.mask_schedule_series[frame_idx]
        if anim_args.use_noise_mask and keys.noise_mask_schedule_series[frame_idx] is not None:
            noise_mask_seq = keys.noise_mask_schedule_series[frame_idx]
        
        if args.use_mask and not anim_args.use_noise_mask:
            noise_mask_seq = mask_seq
        
        depth = None

        if anim_args.animation_mode == '3D' and (cmd_opts.lowvram or cmd_opts.medvram):
            # Unload the main checkpoint and load the depth model
            lowvram.send_everything_to_cpu()
            sd_hijack.model_hijack.undo_hijack(sd_model)
            devices.torch_gc()
            depth_model.to(root.device)
        
        # emit in-between frames
        if turbo_steps > 1:
            tween_frame_start_idx = max(0, frame_idx-turbo_steps)
            cadence_flow = None
            for tween_frame_idx in range(tween_frame_start_idx, frame_idx):
                tween = float(tween_frame_idx - tween_frame_start_idx + 1) / float(frame_idx - tween_frame_start_idx)
                advance_prev = turbo_prev_image is not None and tween_frame_idx > turbo_prev_frame_idx
                advance_next = tween_frame_idx > turbo_next_frame_idx

                # optical flow cadence setup before animation warping
                if anim_args.animation_mode == '3D' and anim_args.optical_flow_cadence != 'None':
                    if keys.strength_schedule_series[tween_frame_start_idx] > 0:
                        if cadence_flow is None and turbo_prev_image is not None and turbo_next_image is not None:
                            cadence_flow = get_flow_from_images(turbo_prev_image, turbo_next_image, anim_args.optical_flow_cadence) / 2
                            turbo_next_image = image_transform_optical_flow(turbo_next_image, -cadence_flow, 1)

                print(f"Creating in-between {'' if cadence_flow is None else anim_args.optical_flow_cadence + ' optical flow '}cadence frame: {tween_frame_idx}; tween:{tween:0.2f};")

                if depth_model is not None:
                    assert(turbo_next_image is not None)
                    depth = depth_model.predict(turbo_next_image, anim_args.midas_weight, root.half_precision)
                    
                if advance_prev:
                    turbo_prev_image, _ = anim_frame_warp(turbo_prev_image, args, anim_args, keys, tween_frame_idx, depth_model, depth=depth, device=root.device, half_precision=root.half_precision)
                if advance_next:
                    turbo_next_image, _ = anim_frame_warp(turbo_next_image, args, anim_args, keys, tween_frame_idx, depth_model, depth=depth, device=root.device, half_precision=root.half_precision)

                # do optical flow cadence after animation warping
                if cadence_flow is not None:
                    cadence_flow = abs_flow_to_rel_flow(cadence_flow)
                    cadence_flow, _ = anim_frame_warp(cadence_flow, args, anim_args, keys, tween_frame_idx, depth_model, depth=depth, device=root.device, half_precision=root.half_precision)
                    cadence_flow_inc = rel_flow_to_abs_flow(cadence_flow) * tween
                    if advance_prev:
                        turbo_prev_image = image_transform_optical_flow(turbo_prev_image, cadence_flow_inc, 1)
                    if advance_next:
                        turbo_next_image = image_transform_optical_flow(turbo_next_image, cadence_flow_inc, 1)

                # hybrid video motion - warps turbo_prev_image or turbo_next_image to match motion
                if tween_frame_idx > 0:
                    if anim_args.hybrid_motion in ['Affine', 'Perspective']:
                        if anim_args.hybrid_motion_use_prev_img:
                            if advance_prev:
                                matrix = get_matrix_for_hybrid_motion_prev(tween_frame_idx, (args.W, args.H), inputfiles, turbo_prev_image, anim_args.hybrid_motion)
                                turbo_prev_image = image_transform_ransac(turbo_prev_image, matrix, anim_args.hybrid_motion, cv2_border_mode)
                            if advance_next:
                                matrix = get_matrix_for_hybrid_motion_prev(tween_frame_idx, (args.W, args.H), inputfiles, turbo_next_image, anim_args.hybrid_motion)
                                turbo_next_image = image_transform_ransac(turbo_next_image, matrix, anim_args.hybrid_motion, cv2_border_mode)
                        else:
                            matrix = get_matrix_for_hybrid_motion(tween_frame_idx-1, (args.W, args.H), inputfiles, anim_args.hybrid_motion)
                            if advance_prev:
                                turbo_prev_image = image_transform_ransac(turbo_prev_image, matrix, anim_args.hybrid_motion, cv2_border_mode)
                            if advance_next:
                                turbo_next_image = image_transform_ransac(turbo_next_image, matrix, anim_args.hybrid_motion, cv2_border_mode)
                    if anim_args.hybrid_motion in ['Optical Flow']:
                        if anim_args.hybrid_motion_use_prev_img:
                            if advance_prev:
                                flow = get_flow_for_hybrid_motion_prev(tween_frame_idx-1, (args.W, args.H), inputfiles, hybrid_frame_path, prev_flow, turbo_prev_image, anim_args.hybrid_flow_method, anim_args.hybrid_comp_save_extra_frames)
                                turbo_prev_image = image_transform_optical_flow(turbo_prev_image, flow, hybrid_comp_schedules['flow_factor'], cv2_border_mode)
                            if advance_next:
                                flow = get_flow_for_hybrid_motion_prev(tween_frame_idx-1, (args.W, args.H), inputfiles, hybrid_frame_path, prev_flow, turbo_next_image, anim_args.hybrid_flow_method, anim_args.hybrid_comp_save_extra_frames)
                                turbo_next_image = image_transform_optical_flow(turbo_next_image, flow, hybrid_comp_schedules['flow_factor'], cv2_border_mode)
                        else:
                            flow = get_flow_for_hybrid_motion(tween_frame_idx-1, (args.W, args.H), inputfiles, hybrid_frame_path, prev_flow, anim_args.hybrid_flow_method, anim_args.hybrid_comp_save_extra_frames)
                            if advance_prev:
                                turbo_prev_image = image_transform_optical_flow(turbo_prev_image, flow, hybrid_comp_schedules['flow_factor'], cv2_border_mode)
                            if advance_next:
                                turbo_next_image = image_transform_optical_flow(turbo_next_image, flow, hybrid_comp_schedules['flow_factor'], cv2_border_mode)
                        prev_flow = flow

                turbo_prev_frame_idx = turbo_next_frame_idx = tween_frame_idx

                if turbo_prev_image is not None and tween < 1.0:
                    img = turbo_prev_image*(1.0-tween) + turbo_next_image*tween
                else:
                    img = turbo_next_image

                # intercept and override to grayscale
                if anim_args.color_force_grayscale:
                    img = cv2.cvtColor(img.astype(np.uint8), cv2.COLOR_BGR2GRAY)
                    img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

                filename = f"{args.timestring}_{tween_frame_idx:09}.png"
                cv2.imwrite(os.path.join(args.outdir, filename), img)
                if anim_args.save_depth_maps:
                    depth_model.save(os.path.join(args.outdir, f"{args.timestring}_depth_{tween_frame_idx:09}.png"), depth)
            if turbo_next_image is not None:
                prev_img = turbo_next_image

        # get color match for video outside of prev_img conditional
        hybrid_available = anim_args.hybrid_composite or anim_args.hybrid_motion in ['Optical Flow', 'Affine', 'Perspective']
        if anim_args.color_coherence == 'Video Input' and hybrid_available:
            if int(frame_idx) % int(anim_args.color_coherence_video_every_N_frames) == 0:
                prev_vid_img = Image.open(os.path.join(args.outdir, 'inputframes', get_frame_name(anim_args.video_init_path) + f"{frame_idx+1:09}.jpg"))
                prev_vid_img = prev_vid_img.resize((args.W, args.H), PIL.Image.LANCZOS)
                color_match_sample = np.asarray(prev_vid_img)
                color_match_sample = cv2.cvtColor(color_match_sample, cv2.COLOR_RGB2BGR)

        # apply transforms to previous frame
        if prev_img is not None:
            prev_img, depth = anim_frame_warp(prev_img, args, anim_args, keys, frame_idx, depth_model, depth=None, device=root.device, half_precision=root.half_precision)

            # hybrid video motion - warps prev_img to match motion, usually to prepare for compositing
            if frame_idx > 0:
                if anim_args.hybrid_motion in ['Affine', 'Perspective']:
                    if anim_args.hybrid_motion_use_prev_img:
                        matrix = get_matrix_for_hybrid_motion_prev(frame_idx, (args.W, args.H), inputfiles, prev_img, anim_args.hybrid_motion)
                    else:
                        matrix = get_matrix_for_hybrid_motion(frame_idx-1, (args.W, args.H), inputfiles, anim_args.hybrid_motion)
                    prev_img = image_transform_ransac(prev_img, matrix, anim_args.hybrid_motion, cv2_border_mode)    
                if anim_args.hybrid_motion in ['Optical Flow']:
                    if anim_args.hybrid_motion_use_prev_img:
                        flow = get_flow_for_hybrid_motion_prev(frame_idx-1, (args.W, args.H), inputfiles, hybrid_frame_path, prev_flow, prev_img, anim_args.hybrid_flow_method, anim_args.hybrid_comp_save_extra_frames)
                    else:
                        flow = get_flow_for_hybrid_motion(frame_idx-1, (args.W, args.H), inputfiles, hybrid_frame_path, prev_flow, anim_args.hybrid_flow_method, anim_args.hybrid_comp_save_extra_frames)
                    prev_img = image_transform_optical_flow(prev_img, flow, hybrid_comp_schedules['flow_factor'], cv2_border_mode)
                    prev_flow = flow

            # do hybrid video - composites video frame into prev_img (now warped if using motion)
            if anim_args.hybrid_composite:
                args, prev_img = hybrid_composite(args, anim_args, frame_idx, prev_img, depth_model, hybrid_comp_schedules, root)

            # apply color matching
            if anim_args.color_coherence != 'None':
                if color_match_sample is None:
                    color_match_sample = prev_img.copy()
                else:
                    prev_img = maintain_colors(prev_img, color_match_sample, anim_args.color_coherence)

            # intercept and override to grayscale
            if anim_args.color_force_grayscale:
                prev_img = cv2.cvtColor(prev_img, cv2.COLOR_BGR2GRAY)
                prev_img = cv2.cvtColor(prev_img, cv2.COLOR_GRAY2BGR)

            # apply scaling
            contrast_image = (prev_img * contrast).round().astype(np.uint8)
            # anti-blur
            if amount > 0:
                contrast_image = unsharp_mask(contrast_image, (kernel, kernel), sigma, amount, threshold, mask_image if args.use_mask else None)
            # apply frame noising
            if args.use_mask or anim_args.use_noise_mask:
                args.noise_mask = compose_mask_with_check(root, args, noise_mask_seq, noise_mask_vals, Image.fromarray(cv2.cvtColor(contrast_image, cv2.COLOR_BGR2RGB)))
            noised_image = add_noise(contrast_image, noise, args.seed, anim_args.noise_type,
                            (anim_args.perlin_w, anim_args.perlin_h, anim_args.perlin_octaves, anim_args.perlin_persistence),
                             args.noise_mask, args.invert_mask)

            # use transformed previous frame as init for current
            args.use_init = True
            args.init_sample = Image.fromarray(cv2.cvtColor(noised_image, cv2.COLOR_BGR2RGB))
            args.strength = max(0.0, min(1.0, strength))
        
        args.scale = scale
       
        # Pix2Pix Image CFG Scale - does *nothing* with non pix2pix checkpoints
        args.pix2pix_img_cfg_scale = float(keys.pix2pix_img_cfg_scale_series[frame_idx])

        # grab prompt for current frame
        args.prompt = prompt_series[frame_idx]
        
        if args.seed_behavior == 'schedule' or use_parseq:
            args.seed = int(keys.seed_schedule_series[frame_idx])

        if anim_args.enable_checkpoint_scheduling:
            args.checkpoint = keys.checkpoint_schedule_series[frame_idx]
        else:
            args.checkpoint = None
            
        #SubSeed scheduling
        if anim_args.enable_subseed_scheduling:
            args.subseed = int(keys.subseed_schedule_series[frame_idx])
            args.subseed_strength = float(keys.subseed_strength_schedule_series[frame_idx])

        if use_parseq:
            args.seed_enable_extras = True
            args.subseed = int(keys.subseed_series[frame_idx])
            args.subseed_strength = keys.subseed_strength_series[frame_idx]
        
        max_f = anim_args.max_frames - 1
        pattern = r'`.*?`'
        regex = re.compile(pattern)
        prompt_parsed = args.prompt
        for match in regex.finditer(prompt_parsed):
            matched_string = match.group(0)
            parsed_string = matched_string.replace('t', f'{frame_idx}').replace("max_f" , f"{max_f}").replace('`','')
            parsed_value = numexpr.evaluate(parsed_string)
            prompt_parsed = prompt_parsed.replace(matched_string, str(parsed_value))

        prompt_to_print, *after_neg = prompt_parsed.strip().split("--neg")
        prompt_to_print = prompt_to_print.strip()
        after_neg = "".join(after_neg).strip()

        print(f"\033[32mSeed: \033[0m{args.seed}")
        print(f"\033[35mPrompt: \033[0m{prompt_to_print}")
        if after_neg and after_neg.strip():
            print(f"\033[91mNeg Prompt: \033[0m{after_neg}")
            prompt_to_print += f"--neg {after_neg}"

        # set value back into the prompt
        args.prompt = prompt_to_print

        # grab init image for current frame
        if using_vid_init:
            init_frame = get_next_frame(args.outdir, anim_args.video_init_path, frame_idx, False)
            print(f"Using video init frame {init_frame}")
            args.init_image = init_frame
            args.strength = max(0.0, min(1.0, strength))
        if anim_args.use_mask_video:
            args.mask_file = get_mask_from_file(get_next_frame(args.outdir, anim_args.video_mask_path, frame_idx, True), args)
            args.noise_mask = get_mask_from_file(get_next_frame(args.outdir, anim_args.video_mask_path, frame_idx, True), args)

            mask_vals['video_mask'] = get_mask_from_file(get_next_frame(args.outdir, anim_args.video_mask_path, frame_idx, True), args)

        if args.use_mask:
            args.mask_image = compose_mask_with_check(root, args, mask_seq, mask_vals, args.init_sample) if args.init_sample is not None else None # we need it only after the first frame anyway

        # setting up some arguments for the looper
        loop_args.imageStrength = loopSchedulesAndData.image_strength_schedule_series[frame_idx]
        loop_args.blendFactorMax = loopSchedulesAndData.blendFactorMax_series[frame_idx]
        loop_args.blendFactorSlope = loopSchedulesAndData.blendFactorSlope_series[frame_idx]
        loop_args.tweeningFrameSchedule = loopSchedulesAndData.tweening_frames_schedule_series[frame_idx]
        loop_args.colorCorrectionFactor = loopSchedulesAndData.color_correction_factor_series[frame_idx]
        loop_args.use_looper = loopSchedulesAndData.use_looper
        loop_args.imagesToKeyframe = loopSchedulesAndData.imagesToKeyframe

        if 'img2img_fix_steps' in opts.data and opts.data["img2img_fix_steps"]: # disable "with img2img do exactly x steps" from general setting, as it *ruins* deforum animations
            opts.data["img2img_fix_steps"] = False
        if scheduled_clipskip is not None:
            opts.data["CLIP_stop_at_last_layers"] = scheduled_clipskip
        if scheduled_noise_multiplier is not None:
            opts.data["initial_noise_multiplier"] = scheduled_noise_multiplier
        
        if anim_args.animation_mode == '3D' and (cmd_opts.lowvram or cmd_opts.medvram):
            depth_model.to('cpu')
            devices.torch_gc()
            lowvram.setup_for_low_vram(sd_model, cmd_opts.medvram)
            sd_hijack.model_hijack.hijack(sd_model)
        
        # optical flow redo before generation
        if anim_args.optical_flow_redo_generation and prev_img is not None and strength > 0:
            print("Optical Flow redo creating disposable diffusion before actual diffusion for flow estimate.")
            stored_seed = args.seed
            args.seed = random.randint(0, 2**32 - 1)
            disposable_image = generate(args, keys, anim_args, loop_args, controlnet_args, root, frame_idx, sampler_name=scheduled_sampler_name)
            disposable_image = cv2.cvtColor(np.array(disposable_image), cv2.COLOR_RGB2BGR)
            disposable_image = maintain_colors(prev_img, color_match_sample, anim_args.color_coherence)
            disposable_flow = get_flow_from_images(prev_img, disposable_image, "DIS Fine")
            noised_image = image_transform_optical_flow(noised_image, disposable_flow, 1)
            args.init_sample = Image.fromarray(cv2.cvtColor(noised_image, cv2.COLOR_BGR2RGB))
            args.seed = stored_seed
            del(disposable_image,disposable_flow,stored_seed)
            gc.collect()

        # diffusion redo
        if int(anim_args.diffusion_redo) > 0 and prev_img is not None and strength > 0:
            stored_seed = args.seed
            for n in range(0,int(anim_args.diffusion_redo)):
                args.seed = random.randint(0, 2**32 - 1)
                disposable_image = generate(args, keys, anim_args, loop_args, controlnet_args, root, frame_idx, sampler_name=scheduled_sampler_name)
                disposable_image = cv2.cvtColor(np.array(disposable_image), cv2.COLOR_RGB2BGR)
                # color match on last one only
                if (n == int(anim_args.diffusion_redo)):
                    disposable_image = maintain_colors(prev_img, color_match_sample, anim_args.color_coherence)                
                args.init_sample = Image.fromarray(cv2.cvtColor(disposable_image, cv2.COLOR_BGR2RGB))
                del(disposable_image)
                gc.collect()
            args.seed = stored_seed

        # generation
        image = generate(args, keys, anim_args, loop_args, controlnet_args, root, frame_idx, sampler_name=scheduled_sampler_name)
        patience = 10

        # color matching on first frame is after generation, color match was collected earlier
        if frame_idx == 0:
            if anim_args.color_coherence == 'Image' or (anim_args.color_coherence == 'Video Input' and hybrid_available):
                image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
                image = maintain_colors(image, color_match_sample, anim_args.color_coherence)
                image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(image)

        # intercept and override to grayscale
        if anim_args.color_force_grayscale:
            image = ImageOps.grayscale(image)
            image = ImageOps.colorize(image, black ="black", white ="white")

        # on strength 0, set color match to generation
        if strength == 0 and not anim_args.color_coherence in ['Image', 'Video Input']:
            color_match_sample = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)

        # reroll blank frame 
        if not image.getbbox():
            print("Blank frame detected! If you don't have the NSFW filter enabled, this may be due to a glitch!")
            if args.reroll_blank_frames == 'reroll':
                while not image.getbbox():
                    print("Rerolling with +1 seed...")
                    args.seed += 1
                    image = generate(args, keys, anim_args, loop_args, controlnet_args, root, frame_idx, sampler_name=scheduled_sampler_name)
                    patience -= 1
                    if patience == 0:
                        print("Rerolling with +1 seed failed for 10 iterations! Try setting webui's precision to 'full' and if it fails, please report this to the devs! Interrupting...")
                        state.interrupted = True
                        state.current_image = image
                        return
            elif args.reroll_blank_frames == 'interrupt':
                print("Interrupting to save your eyes...")
                state.interrupted = True
                state.current_image = image
            image = blank_frame_reroll(image, args, root, frame_idx)
            if image == None:
                return

        opencv_image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        if not using_vid_init:
            prev_img = opencv_image

        if turbo_steps > 1:
            turbo_prev_image, turbo_prev_frame_idx = turbo_next_image, turbo_next_frame_idx
            turbo_next_image, turbo_next_frame_idx = opencv_image, frame_idx
            frame_idx += turbo_steps
        else:    
            filename = f"{args.timestring}_{frame_idx:09}.png"
            save_image(image, 'PIL', filename, args, video_args, root)

            if anim_args.save_depth_maps:
                if cmd_opts.lowvram or cmd_opts.medvram:
                    lowvram.send_everything_to_cpu()
                    sd_hijack.model_hijack.undo_hijack(sd_model)
                    devices.torch_gc()
                    depth_model.to(root.device)
                depth = depth_model.predict(opencv_image, anim_args.midas_weight, root.half_precision)
                depth_model.save(os.path.join(args.outdir, f"{args.timestring}_depth_{frame_idx:09}.png"), depth)
                if cmd_opts.lowvram or cmd_opts.medvram:
                    depth_model.to('cpu')
                    devices.torch_gc()
                    lowvram.setup_for_low_vram(sd_model, cmd_opts.medvram)
                    sd_hijack.model_hijack.hijack(sd_model)
            frame_idx += 1

        state.current_image = image

        args.seed = next_seed(args)
        
    if predict_depths and not keep_in_vram:
        depth_model.delete_model
        if anim_args.midas_weight < 1.0:
            adabins_model.delete_model()