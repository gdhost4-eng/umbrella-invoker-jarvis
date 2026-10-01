--[[
     ~ qLocalization
     ~ automatic localization wrapper for Lua menu interfaces

     ~ author: qfun (qfun_g9s)
]]

local qLocalization = (function()
	local lib = {}

	local a = function(...)
		return ...
	end

	local state = {
		lang = Menu.Find("SettingsHidden", "", "", "", "Main", "Language"),
		instances = {},
	}

	local setters = {
		ToolTip = "tooltip",
	}

	local helpers
	do
		helpers = {
			resolve = a(function(root, path)
				for key in path:gmatch("[^.]+") do
					if type(root) ~= "table" then
						return
					end

					root = root[key]
				end

				return root
			end),

			is_object = a(function(value)
				return type(value) == "table" or type(value) == "userdata"
			end),

			has_method = a(function(object, name)
				return helpers.is_object(object) and type(object[name]) == "function"
			end),

			is_menu_object = a(function(value)
				return helpers.has_method(value, "Name") and helpers.has_method(value, "Type")
			end),

			is_list = a(function(value)
				if type(value) ~= "table" or #value == 0 then
					return false
				end

				for i = 1, #value do
					if type(value[i]) ~= "string" then
						return false
					end
				end

				return true
			end),

			is_indexed_list = a(function(object)
				return helpers.has_method(object, "List") and not helpers.has_method(object, "ListEnabled")
			end),
		}
	end

	function lib.new(translations)
		local languages = {}

		for i, name in ipairs(state.lang and state.lang:List() or {}) do
			local code = name:match("%a+")

			if code and translations[code] then
				languages[i - 1] = code
			end
		end

		local localization = {
			translations = translations,
			languages = languages,
			objects = {},
		}

		local methods
		do
			methods = {
				get_language = a(function(language_index)
					if language_index == nil and state.lang then
						language_index = state.lang:Get()
					end

					return localization.languages[language_index] or "en"
				end),

				localize = a(function(path, language_index)
					if type(path) ~= "string" then
						return path
					end

					local language = methods.get_language(language_index)

					return helpers.resolve(localization.translations[language], path)
						or helpers.resolve(localization.translations.en, path)
						or path
				end),

				has = a(function(path)
					if type(path) ~= "string" then
						return false
					end

					return helpers.resolve(localization.translations.en, path) ~= nil
						or helpers.resolve(localization.translations[methods.get_language()], path) ~= nil
				end),

				localize_items = a(function(items, language_index)
					local result, localized = {}, false

					for i = 1, #items do
						local value = items[i]

						if methods.has(value) then
							result[i] = methods.localize(value, language_index)
							localized = true
						else
							result[i] = value
						end
					end

					return result, localized
				end),

				apply = a(function(object, kind, path, language_index)
					if kind == "label" then
						object:ForceLocalization(methods.localize(path, language_index))
					elseif kind == "tooltip" then
						object:ToolTip(methods.localize(path, language_index))
					elseif kind == "items" then
						local value = object:Get()

						object:Update((methods.localize_items(path, language_index)))
						object:Set(value)
					end
				end),

				track = a(function(object, kind, path, apply_now)
					local record = localization.objects[object]

					if record == nil then
						record = {}
						localization.objects[object] = record
					end

					record[kind] = path

					if apply_now then
						methods.apply(object, kind, path)
					end
				end),

				register = a(function(object, path)
					if not methods.has(path) or not helpers.has_method(object, "ForceLocalization") then
						return
					end

					methods.track(object, "label", path, true)
				end),

				update = a(function(language_index)
					for object, record in pairs(localization.objects) do
						for kind, path in pairs(record) do
							methods.apply(object, kind, path, language_index)
						end
					end
				end),

				wrap = a(function(target, bind_self)
					if not helpers.is_object(target) then
						return target
					end

					local proxy

					proxy = setmetatable({}, {
						__index = function(_, key)
							local member = target[key]

							if type(member) ~= "function" then
								return member
							end

							return function(...)
								local args = table.pack(...)

								if bind_self and args[1] == proxy then
									table.remove(args, 1)
									args.n = args.n - 1
								end

								if key == "Switch" and args.n < 2 then
									args[2] = false
									args.n = 2
								end

								local name_path, item_paths

								if setters[key] then
									if methods.has(args[1]) then
										methods.track(target, setters[key], args[1], false)

										args[1] = methods.localize(args[1])
									end
								else
									local name_index = bind_self and 1 or args.n

									if methods.has(args[name_index]) then
										name_path = args[name_index]
									end

									local items_index

									if key == "Combo" then
										items_index = 2
									elseif key == "Update" and helpers.is_indexed_list(target) then
										items_index = 1
									end

									if items_index ~= nil and helpers.is_list(args[items_index]) then
										local items, localized = methods.localize_items(args[items_index])

										if localized then
											item_paths = args[items_index]
											args[items_index] = items
										end
									end
								end

								local results

								if bind_self then
									results = table.pack(member(target, table.unpack(args, 1, args.n)))
								else
									results = table.pack(member(table.unpack(args, 1, args.n)))
								end

								for i = 1, results.n do
									local result = results[i]

									if helpers.is_menu_object(result) then
										if name_path then
											methods.register(result, name_path)
										end

										if item_paths then
											methods.track(result, "items", item_paths, false)
											item_paths = nil
										end

										results[i] = methods.wrap(result, true)
									end
								end

								if item_paths then
									methods.track(target, "items", item_paths, false)
								end

								return table.unpack(results, 1, results.n)
							end
						end,

						__newindex = function(_, key, value)
							target[key] = value
						end,
					})

					return proxy
				end),
			}
		end

		state.instances[methods] = true

		return {
			GetLanguage = methods.get_language,

			Get = methods.localize,
			Localize = methods.localize,

			Update = methods.update,
			Register = methods.register,

			Wrap = methods.wrap,

			WrapLibrary = function(library)
				return methods.wrap(library, false)
			end,
		}
	end

	if state.lang then
		state.lang:SetCallback(function(this)
			local language_index = this:Get()

			for methods in pairs(state.instances) do
				methods.update(language_index)
			end
		end, true)
	else
		Log.Write("[qLocalization] Language widget not found, using English fallback")
	end

	return lib
end)()

local localization = qLocalization.new({
	en = {
		jv_group_main = "Main",
		jv_group_cast = "Casting",
		jv_enable = "Enable",
		jv_enable_tip = "Runs voice commands from the Jarvis helper.\nDownload Jarvis.exe from Releases\nand run it",
		jv_gear_extra = "Extra",
		jv_port = "Helper port",
		jv_port_tip = "Same as bridge.port in config.yaml",
		jv_debug = "Debug log",
		jv_debug_tip = "Writes every command and what\nthe script did with it to the log",
		jv_wake_key = "Wake up",
		jv_wake_key_tip = "Same as saying the wake word:\nJarvis listens without it for a while",
		jv_mute_key = "Mute",
		jv_mute_key_tip = "Turns mute on and off",
		jv_wake_bind = "Jarvis: wake up",
		jv_mute_bind = "Jarvis: mute",
		jv_orb = "Orb",
		jv_orb_tip = "Shows up while Jarvis is listening.\nDrag it while this menu is open",
		jv_gear_orb = "Orb look",
		jv_orb_size = "Size",
		jv_orb_alpha = "Opacity",
		jv_orb_offline = "Show without the helper",
		jv_orb_offline_tip = "A small grey dot while the helper\nis not running",
		jv_orb_match = "Only in a match",
		jv_spells = "Cast at cursor: spells",
		jv_spells_tip = "An unchecked spell opens the targeting\ncursor and you click the target yourself",
		jv_items = "Cast at cursor: items",
		jv_items_tip = "An unchecked item opens the targeting\ncursor and you click the target yourself",
		jv_other = "Slots and other items",
		jv_other_tip = "For slot commands and items\nmissing from the list above",
		jv_others_cursor = "At cursor",
		jv_others_aim = "Targeting cursor",
		jv_target = "Target near cursor",
		jv_target_tip = "Who unit spells and items pick\nnear the cursor",
		jv_targets_hero = "Hero",
		jv_targets_unit = "Any unit",
		jv_gear_target = "Target search",
		jv_radius = "Search radius",
		jv_radius_tip = "How far from the cursor\na target can be",
		jv_no_target = "No target near cursor",
		jv_no_targets_aim = "Targeting cursor",
		jv_no_targets_skip = "Do nothing",
		jv_cd_check = "Skip if on cooldown",
		jv_cd_check_tip = "Refuses the command if the spell,\nitem or Invoke is on cooldown",
		jv_restore = "Restore orbs",
		jv_restore_tip = "After a cast puts back the orbs\nchosen by voice or in the gear",
		jv_timing = "Timings",
		jv_gear_timing = "Timings",
		jv_confirm = "Invoke confirm, ms",
		jv_confirm_tip = "How long to wait for the spell\nto appear in the slot",
		jv_retries = "Invoke retries",
		jv_retries_tip = "How many times to press the orbs and\nInvoke again if the spell did not appear",
		jv_invoke_wait = "Wait for Invoke, ms",
		jv_invoke_wait_tip = "If Invoke comes off cooldown sooner,\nwait for it instead of refusing",
		jv_max_age = "Command lifetime, ms",
		jv_max_age_tip = "An older command is dropped",
		jv_gap = "Pause between commands, ms",
		jv_gap_tip = "Gap between two commands\nfrom one phrase",
		jv_allow = "Allowed commands",
		jv_allow_tip = "Unchecked kinds of commands\nare ignored",
		jv_stance = "Default orbs",
		jv_stance_tip = "Orbs to put back after a cast\nuntil you pick others by voice",
		jv_stances_none = "Do not change",
		jv_stances_quas = "Quas",
		jv_stances_wex = "Wex",
		jv_stances_exort = "Exort",
		jv_gear_restore = "Orbs",
		jv_caption = "Caption under the orb",
		jv_caption_tip = "Shows the last command\nand why it was refused",
		jv_r_off = "turned off in the menu",
		jv_st_offline = "Helper is not running: run Jarvis.exe",
		jv_st_nojson = "assets/JSON.lua is missing",
		jv_st_loading = "Loading",
		jv_st_error = "Error: %s",
		jv_st_muted = "Muted",
		jv_st_listening = "Listening",
		jv_st_sleeping = "Asleep, say: %s",
		jv_r_old = "too old (%d ms), dropped",
		jv_r_not_ready = "not in a match as Invoker",
		jv_r_dead = "hero is dead",
		jv_r_no_ability = "ability not found",
		jv_r_in_slot = "already invoked",
		jv_r_cd = "on cooldown, %.0f s",
		jv_r_orb = "%s is not learned",
		jv_r_invoke_cd = "Invoke on cooldown, %.1f s",
		jv_r_invoke_late = "Invoke did not come off cooldown in time",
		jv_r_invoke_fail = "Invoke was not confirmed",
		jv_r_invoked = "invoked",
		jv_r_no_item = "no such item",
		jv_r_backpack = "in the backpack",
		jv_r_stash = "in the stash",
		jv_r_passive = "passive",
		jv_r_empty = "slot is empty",
		jv_r_no_target = "no target near the cursor",
		jv_r_no_points = "no ability points",
		jv_r_js = "targeting call failed: %s",
		jv_r_cast_cursor = "at cursor",
		jv_r_cast_target = "on %s",
		jv_r_cast_self = "on self",
		jv_r_cast_aim = "targeting cursor",
		jv_r_cast_now = "cast",
		jv_r_orbs = "orbs %s",
		jv_r_learned = "leveled up",
		jv_r_done = "done",
		jv_r_unknown = "unknown command",
		jv_r_combo_on = "started, right click cancels",
		jv_r_combo_rmb = "cancelled by right click",
		jv_r_combo_stop = "stopped",
		jv_r_combo_end = "combo key was released",
		jv_r_combo_fail = "the helper could not press the combo key",
		jv_r_combo_menu = "Invoker combo menu was not found",
		jv_r_combo_mode = "the Invoker menu is set to %d combos",
		jv_r_combo_slot = "no such combo slot",
		jv_r_combo_nokey = "Combo Key is not set in the Invoker menu",
		jv_r_combo_key = "cannot press the key with code %d",
		jv_r_combo_bind = "set the slot key in Binds, Switch Slot",
	},
	ru = {
		jv_group_main = "Основное",
		jv_group_cast = "Каст",
		jv_enable = "Включить",
		jv_enable_tip = "Выполняет голосовые команды помощника Jarvis.\nСкачай Jarvis.exe из Releases\nи запусти его",
		jv_gear_extra = "Дополнительно",
		jv_port = "Порт помощника",
		jv_port_tip = "Тот же, что bridge.port в config.yaml",
		jv_debug = "Отладка в лог",
		jv_debug_tip = "Пишет в лог каждую команду\nи что скрипт с ней сделал",
		jv_wake_key = "Разбудить",
		jv_wake_key_tip = "То же, что сказать «Джарвис»: бот\nкакое-то время слушает без обращения",
		jv_mute_key = "Мут",
		jv_mute_key_tip = "Включает и снимает мут",
		jv_wake_bind = "Jarvis: разбудить",
		jv_mute_bind = "Jarvis: мут",
		jv_orb = "Кружок",
		jv_orb_tip = "Виден, пока Джарвис слушает.\nПеретаскивается при открытом меню",
		jv_gear_orb = "Вид кружка",
		jv_orb_size = "Размер",
		jv_orb_alpha = "Непрозрачность",
		jv_orb_offline = "Показывать без помощника",
		jv_orb_offline_tip = "Маленькая серая точка,\nпока помощник не запущен",
		jv_orb_match = "Только в матче",
		jv_spells = "Сразу в курсор: спеллы",
		jv_spells_tip = "Спелл без галочки открывает прицел,\nцель кликаешь сам",
		jv_items = "Сразу в курсор: предметы",
		jv_items_tip = "Предмет без галочки открывает прицел,\nцель кликаешь сам",
		jv_other = "Слоты и остальные предметы",
		jv_other_tip = "Для команд по слотам и предметов,\nкоторых нет в списке выше",
		jv_others_cursor = "В курсор",
		jv_others_aim = "Прицел",
		jv_target = "Цель у курсора",
		jv_target_tip = "Кого выбирают спеллы и предметы\nс целью рядом с курсором",
		jv_targets_hero = "Герой",
		jv_targets_unit = "Любой юнит",
		jv_gear_target = "Поиск цели",
		jv_radius = "Радиус поиска",
		jv_radius_tip = "Как далеко от курсора\nможет быть цель",
		jv_no_target = "Нет цели у курсора",
		jv_no_targets_aim = "Прицел",
		jv_no_targets_skip = "Ничего не делать",
		jv_cd_check = "Не кастовать на КД",
		jv_cd_check_tip = "Отказ, если спелл, предмет\nили Invoke на КД",
		jv_restore = "Возвращать сферы",
		jv_restore_tip = "После каста возвращает сферы,\nвыбранные голосом или в шестеренке",
		jv_timing = "Тайминги",
		jv_gear_timing = "Тайминги",
		jv_confirm = "Ждать Invoke, мс",
		jv_confirm_tip = "Сколько ждать, пока спелл\nпоявится в слоте",
		jv_retries = "Повторы Invoke",
		jv_retries_tip = "Сколько раз нажать сферы и Invoke\nзаново, если спелл не появился",
		jv_invoke_wait = "Ждать откат Invoke, мс",
		jv_invoke_wait_tip = "Если Invoke откатится быстрее,\nподождать его, а не отказывать",
		jv_max_age = "Срок жизни команды, мс",
		jv_max_age_tip = "Команда старше не выполняется",
		jv_gap = "Пауза между командами, мс",
		jv_gap_tip = "Пауза между двумя командами\nиз одной фразы",
		jv_allow = "Разрешенные команды",
		jv_allow_tip = "Команды без галочки\nне выполняются",
		jv_stance = "Сферы по умолчанию",
		jv_stance_tip = "Какие сферы возвращать после каста,\nпока не выбраны другие голосом",
		jv_stances_none = "Не менять",
		jv_stances_quas = "Quas",
		jv_stances_wex = "Wex",
		jv_stances_exort = "Exort",
		jv_gear_restore = "Сферы",
		jv_caption = "Подпись под кружком",
		jv_caption_tip = "Пишет последнюю команду\nи причину отказа",
		jv_r_off = "выключено в меню",
		jv_st_offline = "Помощник не запущен: запусти Jarvis.exe",
		jv_st_nojson = "Нет файла assets/JSON.lua",
		jv_st_loading = "Загружаюсь",
		jv_st_error = "Ошибка: %s",
		jv_st_muted = "Мут",
		jv_st_listening = "Слушаю",
		jv_st_sleeping = "Сплю, скажи: %s",
		jv_r_old = "устарела (%d мс), пропущена",
		jv_r_not_ready = "не в матче за Invoker",
		jv_r_dead = "герой мертв",
		jv_r_no_ability = "способность не найдена",
		jv_r_in_slot = "уже в слоте",
		jv_r_cd = "на КД, %.0f с",
		jv_r_orb = "сфера %s не изучена",
		jv_r_invoke_cd = "Invoke на КД, %.1f с",
		jv_r_invoke_late = "Invoke не откатился вовремя",
		jv_r_invoke_fail = "Invoke не подтвердился",
		jv_r_invoked = "вызван",
		jv_r_no_item = "нет предмета",
		jv_r_backpack = "в рюкзаке",
		jv_r_stash = "в тайнике",
		jv_r_passive = "пассивный",
		jv_r_empty = "слот пуст",
		jv_r_no_target = "нет цели у курсора",
		jv_r_no_points = "нет очков прокачки",
		jv_r_js = "прицел не открылся: %s",
		jv_r_cast_cursor = "в курсор",
		jv_r_cast_target = "на %s",
		jv_r_cast_self = "на себя",
		jv_r_cast_aim = "прицел",
		jv_r_cast_now = "применено",
		jv_r_orbs = "сферы %s",
		jv_r_learned = "прокачано",
		jv_r_done = "готово",
		jv_r_unknown = "неизвестная команда",
		jv_r_combo_on = "запущено, ПКМ отменяет",
		jv_r_combo_rmb = "отменено правым кликом",
		jv_r_combo_stop = "остановлено",
		jv_r_combo_end = "клавиша комбо отпущена",
		jv_r_combo_fail = "помощник не смог нажать клавишу комбо",
		jv_r_combo_menu = "меню комбо Invoker не найдено",
		jv_r_combo_mode = "в меню Invoker выбрано %d комбо",
		jv_r_combo_slot = "нет такого слота комбо",
		jv_r_combo_nokey = "в меню Invoker не задана Combo Key",
		jv_r_combo_key = "клавишу с кодом %d нажать нечем",
		jv_r_combo_bind = "задай клавишу слота в Binds, Switch Slot",
	},
})

local UI = localization.WrapLibrary(Menu)
local L = localization.Get

local K = {
	VERSION = "1.3.1",
	TAG = "[Jarvis] ",
	CFG = "invoker_jarvis",
	HERO = "npc_dota_hero_invoker",
	DEFAULT_PORT = "52360",
	WAIT_MS = 1000,
	WATCHDOG = 6,
	RETRY = 1,
	SYNC_RETRY = 10,
	SYNC_INTERVAL = 0.05,
	JS_WAIT = 0.05,
	ORB_WAIT = 0.15,
	ORB_BLIND = 0.03,
	CAPTION_TIME = 2.5,
	ALLOW = { "Spells", "Items", "Slots", "Orbs", "Level up", "Stop and hold", "Combos" },
	COMBO_SLOTS = 12,
	COMBO_CONFIRM = 0.8,
	COMBO_LOST = 0.5,
	KEYS = {
		"PAD_DIVIDE", "PAD_MULTIPLY", "PAD_MINUS", "PAD_PLUS", "PAD_ENTER", "PAD_DECIMAL", "LBRACKET", "RBRACKET",
		"SEMICOLON", "APOSTROPHE", "BACKQUOTE", "COMMA", "PERIOD", "SLASH", "BACKSLASH", "MINUS", "EQUAL", "ENTER",
		"SPACE", "BACKSPACE", "TAB", "CAPSLOCK", "NUMLOCK", "ESCAPE", "SCROLLLOCK", "INSERT", "DELETE", "HOME", "END",
		"PAGEUP", "PAGEDOWN", "BREAK", "LSHIFT", "RSHIFT", "LALT", "RALT", "LCONTROL", "RCONTROL", "LWIN", "RWIN",
		"APP", "UP", "LEFT", "DOWN", "RIGHT",
	},
	MOUSE_CODES = { [314] = "KEY_MOUSE1", [315] = "KEY_MOUSE2", [316] = "KEY_MOUSE3", [317] = "KEY_MOUSE4", [318] = "KEY_MOUSE5" },
	STANCES = { [1] = "QQQ", [2] = "WWW", [3] = "EEE" },
	ORB_MOD = {
		modifier_invoker_quas_instance = "Q",
		modifier_invoker_wex_instance = "W",
		modifier_invoker_exort_instance = "E",
	},
	CD_TOLERANCE = 0.3,
	DEFERRED_TIME = 5,
	ORB_NAMES = { Q = "Quas", W = "Wex", E = "Exort" },
	ORB_KEYS = { quas = "Q", wex = "W", exort = "E" },
	SPELLS = {
		cold_snap = { ability = "invoker_cold_snap", orbs = "QQQ", title = "Cold Snap" },
		ghost_walk = { ability = "invoker_ghost_walk", orbs = "QQW", title = "Ghost Walk" },
		ice_wall = { ability = "invoker_ice_wall", orbs = "QQE", title = "Ice Wall" },
		emp = { ability = "invoker_emp", orbs = "WWW", title = "EMP" },
		tornado = { ability = "invoker_tornado", orbs = "WWQ", title = "Tornado" },
		alacrity = { ability = "invoker_alacrity", orbs = "WWE", title = "Alacrity" },
		sun_strike = { ability = "invoker_sun_strike", orbs = "EEE", title = "Sun Strike" },
		forge_spirit = { ability = "invoker_forge_spirit", orbs = "EEQ", title = "Forge Spirit" },
		chaos_meteor = { ability = "invoker_chaos_meteor", orbs = "EEW", title = "Chaos Meteor" },
		deafening_blast = { ability = "invoker_deafening_blast", orbs = "QWE", title = "Deafening Blast" },
	},
	AIMED_SPELLS = { "cold_snap", "tornado", "emp", "alacrity", "sun_strike", "chaos_meteor", "deafening_blast" },
	ITEM_GROUPS = {
		{ id = "TP Scroll", on = false, names = { "item_tpscroll" } },
		{ id = "Blink", on = true, names = { "item_blink", "item_overwhelming_blink", "item_swift_blink", "item_arcane_blink" } },
		{ id = "Eul's", on = true, names = { "item_cyclone" } },
		{ id = "Wind Waker", on = true, names = { "item_wind_waker" } },
		{ id = "Hex", on = true, names = { "item_sheepstick" } },
		{ id = "Dagon", on = true, names = { "item_dagon", "item_dagon_2", "item_dagon_3", "item_dagon_4", "item_dagon_5" } },
		{ id = "Midas", on = true, names = { "item_hand_of_midas" } },
		{ id = "Orchid", on = true, names = { "item_orchid", "item_bloodthorn" } },
		{ id = "Nullifier", on = true, names = { "item_nullifier" } },
		{ id = "Atos", on = true, names = { "item_rod_of_atos", "item_gungir" } },
		{ id = "Force", on = true, names = { "item_force_staff", "item_hurricane_pike" } },
		{ id = "Ethereal Blade", on = true, names = { "item_ethereal_blade" } },
		{ id = "Harpoon", on = true, names = { "item_harpoon" } },
		{ id = "Mjollnir", on = true, names = { "item_mjollnir" } },
		{ id = "Lotus", on = true, names = { "item_lotus_orb" } },
		{ id = "Glimmer", on = true, names = { "item_glimmer_cape" } },
		{ id = "Urn", on = true, names = { "item_urn_of_shadows", "item_spirit_vessel" } },
		{ id = "Salve", on = true, names = { "item_flask" } },
		{ id = "Clarity", on = true, names = { "item_clarity" } },
		{ id = "Observer", on = false, names = { "item_ward_observer", "item_ward_dispenser" } },
		{ id = "Sentry", on = false, names = { "item_ward_sentry" } },
	},
	ITEM_GROUP = {},
	SLOTS = { item1 = 0, item2 = 1, item3 = 2, item4 = 3, item5 = 4, item6 = 5, tp = 15, neutral = 16 },
	SEARCH = { 0, 1, 2, 3, 4, 5, 15, 16, 6, 7, 8, 9, 10, 11, 12, 13, 14 },
	HUD = { "Hud", "DotaHud" },
	JS = "(function(){var c=$.GetContextPanel();var r='ok';try{%s}catch(e){r='err '+e;}"
		.. "if(c&&c.SetAttributeString){c.SetAttributeString('jv_js','%s '+r);}})()",
	JS_AIM = "Abilities.ExecuteAbility(%d,%d,false);",
	JS_SELF = "Abilities.CreateDoubleTapCastOrder(%d,%d);",
	ORB_MIN = 48,
	ORB_MAX = 256,
	HALO = 1.75,
	PALETTES = {
		pearl = {
			blobs = { { 255, 255, 255, 0.7 }, { 165, 185, 255, 0.85 }, { 255, 185, 225, 0.75 } },
			core = { 111, 127, 184 },
			halo = { 200, 190, 255 },
		},
		grey = {
			blobs = { { 225, 225, 232, 0.45 }, { 150, 155, 170, 0.55 }, { 195, 195, 205, 0.45 } },
			core = { 71, 74, 83 },
			halo = { 170, 170, 180 },
		},
		red = {
			blobs = { { 255, 215, 215, 0.65 }, { 255, 110, 120, 0.85 }, { 255, 165, 150, 0.75 } },
			core = { 132, 64, 76 },
			halo = { 255, 110, 120 },
		},
		amber = {
			blobs = { { 255, 245, 220, 0.65 }, { 255, 190, 100, 0.85 }, { 255, 215, 165, 0.7 } },
			core = { 134, 102, 63 },
			halo = { 255, 190, 100 },
		},
	},
	LOOKS = {
		listening = { palette = "pearl", scale = 1.0, opacity = 1.0, speed = 0.6, voice = true, breath = 0, rate = 0, arc = 0 },
		blocked = { palette = "amber", scale = 1.0, opacity = 0.95, speed = 0.6, voice = true, breath = 0, rate = 0, arc = 0 },
		loading = { palette = "pearl", scale = 0.72, opacity = 0.65, speed = 1.1, voice = false, breath = 0.15, rate = 2.0, arc = 1.0 },
		muted = { palette = "grey", scale = 0.72, opacity = 0.8, speed = 0.15, voice = false, breath = 0, rate = 0, arc = 0 },
		error = { palette = "red", scale = 0.85, opacity = 0.95, speed = 0.5, voice = false, breath = 0.25, rate = 3.0, arc = 0 },
		offline = { palette = "grey", scale = 0.4, opacity = 0.45, speed = 0.05, voice = false, breath = 0, rate = 0, arc = 0 },
		preview = { palette = "pearl", scale = 0.85, opacity = 0.7, speed = 0.6, voice = false, breath = 0, rate = 0, arc = 0 },
		sleeping = { scale = 0.6, opacity = 0.0, speed = 0.6, voice = false, breath = 0, rate = 0, arc = 0 },
	},
}

for _, group in ipairs(K.ITEM_GROUPS) do
	for _, name in ipairs(group.names) do K.ITEM_GROUP[name] = group.id end
end

for i = 0, 9 do
	K.KEYS[#K.KEYS + 1] = tostring(i)
	K.KEYS[#K.KEYS + 1] = "PAD_" .. i
end
for i = 1, 24 do K.KEYS[#K.KEYS + 1] = "F" .. i end
for i = 1, 5 do K.KEYS[#K.KEYS + 1] = "MOUSE" .. i end
for i = 65, 90 do K.KEYS[#K.KEYS + 1] = string.char(i) end

local ui = {}

do
	local page
	local found, hero_tab = pcall(UI.Find, "Heroes", "Hero List", "Invoker")
	if found and hero_tab then
		page = hero_tab:Create("Jarvis")
		ui.hero_enable = Menu.Find("Heroes", "Hero List", "Invoker", "Main Settings", "Hero Settings", "Enable")
	else
		local tab = UI.Create("Scripts", "Scripts", "Jarvis")
		tab:Icon("\u{f130}")
		page = tab:Create("Settings")
	end

	local g_main = page:Create("jv_group_main", Enum.GroupSide.Left)
	local g_cast = page:Create("jv_group_cast", Enum.GroupSide.Right)

	ui.enable = g_main:Switch("jv_enable", false, "\u{f011}")
	ui.enable:ToolTip("jv_enable_tip")
	local g_extra = ui.enable:Gear("jv_gear_extra")
	ui.port = g_extra:Input("jv_port", K.DEFAULT_PORT, "\u{f1e6}")
	ui.port:ToolTip("jv_port_tip")
	ui.debug = g_extra:Switch("jv_debug", true, "\u{f188}")
	ui.debug:ToolTip("jv_debug_tip")

	ui.wake_key = g_main:Bind("jv_wake_key", Enum.ButtonCode.KEY_NONE, "\u{f130}")
	ui.wake_key:ToolTip("jv_wake_key_tip")
	ui.wake_key:Properties(L("jv_wake_bind"))

	ui.mute_key = g_main:Bind("jv_mute_key", Enum.ButtonCode.KEY_NONE, "\u{f131}")
	ui.mute_key:ToolTip("jv_mute_key_tip")
	ui.mute_key:Properties(L("jv_mute_bind"))

	ui.orb = g_main:Switch("jv_orb", true, "\u{f111}")
	ui.orb:ToolTip("jv_orb_tip")
	local g_orb = ui.orb:Gear("jv_gear_orb")
	ui.orb_size = g_orb:Slider("jv_orb_size", K.ORB_MIN, K.ORB_MAX, 88, "%d px")
	ui.orb_size:Icon("\u{f065}")
	ui.orb_alpha = g_orb:Slider("jv_orb_alpha", 20, 100, 100, "%d%%")
	ui.orb_alpha:Icon("\u{f043}")
	ui.orb_offline = g_orb:Switch("jv_orb_offline", true, "\u{f1e6}")
	ui.orb_offline:ToolTip("jv_orb_offline_tip")
	ui.orb_match = g_orb:Switch("jv_orb_match", false, "\u{f11b}")
	ui.caption = g_orb:Switch("jv_caption", true, "\u{f036}")
	ui.caption:ToolTip("jv_caption_tip")

	ui.allow = g_main:MultiCombo("jv_allow", K.ALLOW, K.ALLOW)
	ui.allow:Icon("\u{f00c}")
	ui.allow:ToolTip("jv_allow_tip")

	ui.cd_check = g_main:Switch("jv_cd_check", false, "\u{f017}")
	ui.cd_check:ToolTip("jv_cd_check_tip")

	ui.restore = g_main:Switch("jv_restore", true, "\u{f2f1}")
	ui.restore:ToolTip("jv_restore_tip")
	local g_restore = ui.restore:Gear("jv_gear_restore")
	ui.stance = g_restore:Combo("jv_stance", { "jv_stances_none", "jv_stances_quas", "jv_stances_wex", "jv_stances_exort" }, 0)
	ui.stance:Icon("\u{f111}")
	ui.stance:ToolTip("jv_stance_tip")

	ui.timing = g_main:Label("jv_timing", "\u{f1de}")
	local g_timing = ui.timing:Gear("jv_gear_timing")
	ui.confirm = g_timing:Slider("jv_confirm", 100, 1000, 300, "%d")
	ui.confirm:Icon("\u{f254}")
	ui.confirm:ToolTip("jv_confirm_tip")
	ui.retries = g_timing:Slider("jv_retries", 0, 3, 1, "%d")
	ui.retries:Icon("\u{f01e}")
	ui.retries:ToolTip("jv_retries_tip")
	ui.invoke_wait = g_timing:Slider("jv_invoke_wait", 0, 3000, 1000, "%d")
	ui.invoke_wait:Icon("\u{f017}")
	ui.invoke_wait:ToolTip("jv_invoke_wait_tip")
	ui.max_age = g_timing:Slider("jv_max_age", 1000, 8000, 4000, "%d")
	ui.max_age:Icon("\u{f1da}")
	ui.max_age:ToolTip("jv_max_age_tip")
	ui.gap = g_timing:Slider("jv_gap", 0, 300, 50, "%d")
	ui.gap:Icon("\u{f04c}")
	ui.gap:ToolTip("jv_gap_tip")

	local spells = {}
	for i, id in ipairs(K.AIMED_SPELLS) do
		local spell = K.SPELLS[id]
		spells[i] = { spell.title, "panorama/images/spellicons/" .. spell.ability .. "_png.vtex_c", true }
	end
	ui.spells = g_cast:MultiSelect("jv_spells", spells, true)
	ui.spells:ToolTip("jv_spells_tip")

	local items = {}
	for i, group in ipairs(K.ITEM_GROUPS) do
		items[i] = { group.id, "panorama/images/items/" .. group.names[1]:sub(6) .. "_png.vtex_c", group.on }
	end
	ui.items = g_cast:MultiSelect("jv_items", items, false)
	ui.items:ToolTip("jv_items_tip")

	ui.other = g_cast:Combo("jv_other", { "jv_others_cursor", "jv_others_aim" }, 0)
	ui.other:Icon("\u{f0b1}")
	ui.other:ToolTip("jv_other_tip")

	ui.target = g_cast:Combo("jv_target", { "jv_targets_hero", "jv_targets_unit" }, 0)
	ui.target:Icon("\u{f05b}")
	ui.target:ToolTip("jv_target_tip")
	local g_target = ui.target:Gear("jv_gear_target")
	ui.radius = g_target:Slider("jv_radius", 100, 1500, 400, "%d")
	ui.radius:Icon("\u{f1ce}")
	ui.radius:ToolTip("jv_radius_tip")
	ui.no_target = g_target:Combo("jv_no_target", { "jv_no_targets_aim", "jv_no_targets_skip" }, 0)
	ui.no_target:Icon("\u{f05e}")
end

local function refresh_disabled()
	local off = not ui.enable:Get()
	ui.port:Disabled(off)
	ui.debug:Disabled(off)
	ui.wake_key:Disabled(off)
	ui.mute_key:Disabled(off)
	ui.orb:Disabled(off)
	local no_orb = off or not ui.orb:Get()
	ui.orb_size:Disabled(no_orb)
	ui.orb_alpha:Disabled(no_orb)
	ui.orb_offline:Disabled(no_orb)
	ui.orb_match:Disabled(no_orb)
	ui.caption:Disabled(no_orb)
	ui.allow:Disabled(off)
	ui.cd_check:Disabled(off)
	ui.restore:Disabled(off)
	ui.stance:Disabled(off or not ui.restore:Get())
	ui.timing:Disabled(off)
	ui.confirm:Disabled(off)
	ui.retries:Disabled(off)
	ui.invoke_wait:Disabled(off)
	ui.max_age:Disabled(off)
	ui.gap:Disabled(off)
	ui.spells:Disabled(off)
	ui.items:Disabled(off)
	ui.other:Disabled(off)
	ui.target:Disabled(off)
	ui.radius:Disabled(off)
	ui.no_target:Disabled(off)
end

ui.enable:SetCallback(refresh_disabled, true)
ui.orb:SetCallback(refresh_disabled)
ui.restore:SetCallback(refresh_disabled)

local function log(text)
	if ui.debug:Get() then Log.Write(K.TAG .. text) end
end

Log.Write(K.TAG .. "v" .. K.VERSION .. " loaded")

local json_ok, JSON = pcall(require, "assets.JSON")
if not json_ok or type(JSON) ~= "table" then
	JSON = nil
	Log.Write(K.TAG .. "assets.JSON is missing, the helper link is off")
end

local floor, min, max, abs, sin, cos, exp, pi = math.floor, math.min, math.max, math.abs, math.sin, math.cos, math.exp, math.pi
local ORDER = Enum.UnitOrder
local ISSUER = Enum.PlayerOrderIssuer.DOTA_ORDER_ISSUER_PASSED_UNIT_ONLY
local BEHAVIOR = Enum.AbilityBehavior

local state = {
	hero = nil,
	hero_index = nil,
	ab = nil,
	in_game = false,
	ready = false,
	queue = {},
	job = nil,
	deferred = nil,
	stance = nil,
	next_job_at = 0,
	link = {
		session = "",
		after = 0,
		state_v = -1,
		pending = false,
		in_call = false,
		sync = false,
		sent_at = 0,
		next_at = 0,
		req = 0,
		online = false,
		why = nil,
		activity = "offline",
		level = 0,
		error = "",
		wake = "",
		results = {},
	},
	js = { hud = nil, n = 0 },
	caption = nil,
	mods_logged = false,
	combo = nil,
	rmb = nil,
	keys = nil,
	key_seq = 0,
	orb = {
		palette = nil,
		scale = K.LOOKS.sleeping.scale,
		velocity = 0,
		opacity = 0,
		speed = 0.6,
		phase = 0,
		amp = 0,
		arc = 0,
		time = 0,
		last = nil,
		x = nil,
		y = nil,
		drag = nil,
		look = nil,
		mouse = false,
		font = nil,
	},
	last_error = nil,
}

local function clamp(v, lo, hi)
	if v < lo then return lo end
	if v > hi then return hi end
	return v
end

local function has(value, flag)
	return (math.tointeger(value) or 0) & flag ~= 0
end

local function enabled()
	return ui.enable:Get() and (not ui.hero_enable or ui.hero_enable:Get())
end

local function linked()
	return JSON ~= nil and enabled() and (not state.in_game or state.hero ~= nil)
end

local function clear_jobs()
	state.queue = {}
	state.job = nil
	state.deferred = nil
	if state.combo then state.combo.stop = true end
end

local function report(label, ok, message, latency, deferred)
	local results = state.link.results
	if #results < 32 then
		results[#results + 1] = { label = label, ok = ok, message = message, latency_ms = latency, deferred = deferred or nil }
	end
	state.caption = { text = tostring(label) .. ": " .. tostring(message), ok = ok, at = os.clock() }
	log(string.format("%s %s: %s%s", ok and "ok" or "FAIL", tostring(label), tostring(message),
		latency and string.format(" (%d ms)", floor(latency)) or ""))
end

local function finish(job, ok, message)
	local latency
	if job.ordered_at then
		latency = (job.ordered_at - job.recv_at) * 1000 + (job.speech_age_ms or job.age_ms)
	end
	report(job.label, ok, message, latency)
	if state.job == job then state.job = nil end
	if job.ordered_at then state.next_job_at = os.clock() + ui.gap:Get() / 1000 end
end

local function mark_order(job)
	if job and not job.ordered_at then job.ordered_at = os.clock() end
end

local function url()
	local port = tonumber(ui.port:Get()) or tonumber(K.DEFAULT_PORT)
	return "http://127.0.0.1:" .. floor(port) .. "/poll"
end

local function set_online(online, why)
	local link = state.link
	if link.online ~= online then
		log(online and "helper connected" or ("helper offline: " .. tostring(why)))
	end
	link.online = online
	link.why = why
	if not online then
		link.activity = "offline"
		link.level = 0
	end
end

local function category(cmd)
	local kind, bind = cmd.kind, cmd.bind
	if kind == "spell" then return "Spells" end
	if kind == "item" then return "Items" end
	if kind == "stance" then return "Orbs" end
	if kind == "learn" then return "Level up" end
	if kind == "combo" then return "Combos" end
	if kind ~= "press" then return nil end
	if bind == "stop" or bind == "hold" then return "Stop and hold" end
	if K.ORB_KEYS[bind] or bind == "invoke" then return "Orbs" end
	return "Slots"
end

local function accept(cmd, now)
	if cmd.kind == "cancel" then
		clear_jobs()
		log("cancel: queue cleared")
		return
	end
	log(string.format("command #%s %s %s age=%sms", tostring(cmd.seq), tostring(cmd.kind), tostring(cmd.label), tostring(cmd.age_ms)))
	if not state.ready then
		report(cmd.label, false, L("jv_r_not_ready"))
		return
	end
	local kind = category(cmd)
	if kind and not ui.allow:Get(kind) then
		report(cmd.label, false, L("jv_r_off"))
		return
	end
	state.queue[#state.queue + 1] = {
		cmd = cmd,
		label = cmd.label or cmd.kind,
		recv_at = now,
		age_ms = tonumber(cmd.age_ms) or 0,
		speech_age_ms = tonumber(cmd.speech_age_ms),
	}
end

local function on_reply(resp)
	local link = state.link
	if type(resp) ~= "table" or tonumber(resp.param) ~= link.req then return end
	link.pending = false
	if not linked() then return end
	if link.in_call and not link.sync then
		link.sync = true
		log("http answers inside the call, switching to short polling")
	end
	local now = os.clock()
	local data
	if tostring(resp.code) == "200" and type(resp.response) == "string" and resp.response ~= "" then
		local ok, decoded = pcall(JSON.decode, JSON, resp.response)
		if ok and type(decoded) == "table" then data = decoded end
	end
	if not data then
		set_online(false, string.format("code=%s error=%s %s", tostring(resp.code), tostring(resp.error_code), tostring(resp.error_message)))
		link.next_at = now + (link.sync and K.SYNC_RETRY or K.RETRY)
		return
	end
	set_online(true)
	if data.session ~= link.session then
		link.session = data.session
		link.after = tonumber(data.seq) or 0
		clear_jobs()
		log("helper session " .. tostring(data.session))
	else
		for _, cmd in ipairs(data.commands or {}) do
			local seq = tonumber(cmd.seq) or 0
			if seq > link.after then
				link.after = seq
				accept(cmd, now)
			end
		end
	end
	link.state_v = tonumber(data.state_v) or -1
	local st = type(data.state) == "table" and data.state or {}
	if st.activity ~= link.activity then log("helper state: " .. tostring(st.activity)) end
	link.activity = st.activity or "sleeping"
	link.level = tonumber(st.level) or 0
	link.error = st.error or ""
	link.wake = st.wake or ""
	link.next_at = link.sync and (now + K.SYNC_INTERVAL) or now
end

local function send(body, tag)
	local link = state.link
	local ok, text = pcall(JSON.encode, JSON, body)
	if not ok then
		log("json encode failed: " .. tostring(text))
		return false
	end
	link.in_call = true
	local sent = HTTP.Request("POST", url(), { headers = { ["Content-Type"] = "application/json" }, data = text }, on_reply, tag)
	link.in_call = false
	return sent
end

local function poll(now)
	local link = state.link
	if link.pending then
		if now - link.sent_at < K.WATCHDOG then return end
		link.pending = false
		set_online(false, "no answer")
	end
	if now < link.next_at then return end
	local body = { session = link.session, after = link.after, state_v = link.state_v, wait_ms = link.sync and 0 or K.WAIT_MS }
	if #link.results > 0 then
		body.results = link.results
		link.results = {}
	end
	link.req = link.req + 1
	link.pending = true
	link.sent_at = now
	if not send(body, tostring(link.req)) then
		link.pending = false
		link.next_at = now + K.RETRY
		link.results = body.results or link.results
		set_online(false, "request was not sent")
	end
end

local function send_action(action)
	log("action " .. action)
	send({ actions = { action }, wait_ms = 0 }, "action")
end

local function send_keys(steps)
	state.key_seq = state.key_seq + 1
	send({ keys = steps, keys_seq = state.key_seq, wait_ms = 0 }, "keys")
end

local function key_name(code)
	if not state.keys then
		state.keys = {}
		for _, name in ipairs(K.KEYS) do
			local ok, value = pcall(function() return Enum.ButtonCode["KEY_" .. name] end)
			value = ok and math.tointeger(value) or 0
			if value > 0 then state.keys[value] = "KEY_" .. name end
		end
	end
	return state.keys[code] or K.MOUSE_CODES[code]
end

local function bind_keys(bind)
	local ok, first, second = pcall(function() return bind:Buttons() end)
	if not ok then return nil, -1 end
	local names = {}
	for _, code in ipairs({ math.tointeger(first) or 0, math.tointeger(second) or 0 }) do
		if code > 0 then
			local name = key_name(code)
			if not name then return nil, code end
			names[#names + 1] = name
		end
	end
	return names
end

local function invoker_widget(tab, group, name)
	local ok, widget = pcall(Menu.Find, "Heroes", "Hero List", "Invoker", tab, group, name)
	return ok and widget or nil
end

local function combo_count()
	local widget = invoker_widget("Panels Settings", "Combo Builder", "Combo Slots")
	if not widget then return K.COMBO_SLOTS end
	local ok, count = pcall(function() return tonumber(tostring(widget:List()[widget:Get() + 1]):match("%d+")) end)
	return ok and count or K.COMBO_SLOTS
end

local function combo_down(combo)
	local ok, down = pcall(function() return combo.bind:IsDown() end)
	return ok and down
end

local function stop_combo(message, ok)
	local combo = state.combo
	if not combo then return end
	state.combo = nil
	send_keys({ { op = "up" } })
	local job = state.job
	if job and job.stage == "combo" then
		finish(job, false, message)
	else
		report(combo.label, ok ~= false, message)
	end
end

local function combo_tick(now)
	local combo = state.combo
	local rmb = Input.IsKeyDown(Enum.ButtonCode.KEY_MOUSE2, true)
	local clicked = rmb and state.rmb == false
	state.rmb = rmb
	if combo.stop or not state.ready then return stop_combo(L("jv_r_combo_stop")) end
	if clicked and combo.rmb then return stop_combo(L("jv_r_combo_rmb")) end
	if not Entity.IsAlive(state.hero) then return stop_combo(L("jv_r_dead"), false) end
	if not combo.seen then return end
	if combo_down(combo) then
		combo.down_at = now
	elseif now - combo.down_at > K.COMBO_LOST then
		stop_combo(L("jv_r_combo_end"))
	end
end

local function combo_guard(now)
	if not state.combo then
		state.rmb = nil
		return
	end
	local ok, err = pcall(combo_tick, now)
	if ok then return end
	state.combo = nil
	send_keys({ { op = "up" } })
	Log.Write(K.TAG .. "combo: " .. tostring(err))
end

local function hud_panel()
	local hud = state.js.hud
	if hud and hud:IsValid() then return hud end
	state.js.hud = nil
	for _, name in ipairs(K.HUD) do
		local ok, panel = pcall(Panorama.GetPanelByName, name, false)
		if ok and panel and panel:IsValid() then
			state.js.hud = panel
			return panel
		end
	end
	return nil
end

local function run_js(template, ability)
	local js = state.js
	js.n = js.n + 1
	local tag = "t" .. js.n
	local code = string.format(template, Entity.GetIndex(ability), Entity.GetIndex(state.hero))
	local hud = hud_panel()
	local script = string.format(K.JS, code, tag)
	local sent
	if hud then
		sent = Engine.RunScript(script, hud)
	else
		sent = Engine.RunScript(script)
	end
	log(string.format("js %s sent=%s hud=%s %s", tag, tostring(sent), tostring(hud ~= nil), code))
	return tag
end

local function js_result(tag)
	local hud = hud_panel()
	if not hud then return true, "no hud panel" end
	local ok, attr = pcall(function() return hud:GetAttribute("jv_js", "") end)
	attr = ok and attr or ""
	if attr == tag .. " ok" then return true, attr end
	return false, attr ~= "" and attr or "no answer"
end

local function load_abilities(hero)
	local ab = {
		Q = NPC.GetAbility(hero, "invoker_quas"),
		W = NPC.GetAbility(hero, "invoker_wex"),
		E = NPC.GetAbility(hero, "invoker_exort"),
		invoke = NPC.GetAbility(hero, "invoker_invoke"),
		spells = {},
	}
	if not (ab.Q and ab.W and ab.E and ab.invoke) then return nil end
	for id, spell in pairs(K.SPELLS) do
		ab.spells[id] = NPC.GetAbility(hero, spell.ability)
	end
	return ab
end

local function order_no_target(ability)
	Player.PrepareUnitOrders(Players.GetLocal(), ORDER.DOTA_UNIT_ORDER_CAST_NO_TARGET, nil, Vector(0, 0, 0),
		ability, ISSUER, state.hero, false, false, false, true, "jarvis")
end

local function press_orbs(orbs)
	for i = 1, #orbs do order_no_target(state.ab[orbs:sub(i, i)]) end
end

local function orbs_ready(orbs)
	local want = { Q = 0, W = 0, E = 0 }
	for i = 1, #orbs do want[orbs:sub(i, i)] = want[orbs:sub(i, i)] + 1 end
	local count, stacks, seen = { Q = 0, W = 0, E = 0 }, { Q = 0, W = 0, E = 0 }, false
	for _, modifier in ipairs(NPC.GetModifiers(state.hero) or {}) do
		local orb = K.ORB_MOD[Modifier.GetName(modifier)]
		if orb then
			seen = true
			count[orb] = count[orb] + 1
			stacks[orb] = stacks[orb] + max(1, Modifier.GetStackCount(modifier) or 0)
		end
	end
	if not seen then return nil end
	return (count.Q == want.Q and count.W == want.W and count.E == want.E)
		or (stacks.Q == want.Q and stacks.W == want.W and stacks.E == want.E)
end

local function log_modifiers()
	if state.mods_logged or not ui.debug:Get() then return end
	state.mods_logged = true
	local names = {}
	for _, modifier in ipairs(NPC.GetModifiers(state.hero) or {}) do
		names[#names + 1] = Modifier.GetName(modifier) .. "x" .. tostring(Modifier.GetStackCount(modifier))
	end
	log("orbs were not seen in modifiers: " .. table.concat(names, " "))
end

local function current_stance()
	return state.stance or K.STANCES[ui.stance:Get()]
end

local function same_orbs(a, b)
	local count = { Q = 0, W = 0, E = 0 }
	for i = 1, #a do count[a:sub(i, i)] = count[a:sub(i, i)] + 1 end
	for i = 1, #b do count[b:sub(i, i)] = count[b:sub(i, i)] - 1 end
	return count.Q == 0 and count.W == 0 and count.E == 0
end

local function unlearned_orb(orbs)
	for i = 1, #orbs do
		local orb = orbs:sub(i, i)
		if Ability.GetLevel(state.ab[orb]) <= 0 then return K.ORB_NAMES[orb] end
	end
	return nil
end

local function spell_of(ability)
	local name = Ability.GetName(ability)
	for id, spell in pairs(K.SPELLS) do
		if spell.ability == name then return id end
	end
	return nil
end

local function invoked_slots()
	local list = {}
	for id, ability in pairs(state.ab.spells) do
		if ability and not Ability.IsHidden(ability) then
			list[#list + 1] = { id = id, ability = ability, index = Ability.GetIndex(ability) }
		end
	end
	table.sort(list, function(a, b) return a.index < b.index end)
	return list
end

local function instant_for(ability)
	local name = Ability.GetName(ability)
	local spell = spell_of(ability)
	if spell then
		for _, id in ipairs(K.AIMED_SPELLS) do
			if id == spell then return ui.spells:Get(K.SPELLS[id].title) end
		end
		return true
	end
	local group = K.ITEM_GROUP[name]
	if group then return ui.items:Get(group) end
	return ui.other:Get() == 0
end

local function find_target(ability)
	local hero = state.hero
	local team = math.tointeger(Ability.GetTargetTeam(ability)) or 0
	local kind = math.tointeger(Ability.GetTargetType(ability)) or 0
	local TYPE, TEAM = Enum.TargetType, Enum.TargetTeam
	local heroes = kind & (TYPE.DOTA_UNIT_TARGET_HERO | TYPE.DOTA_UNIT_TARGET_CUSTOM) ~= 0
	if not heroes and kind & TYPE.DOTA_UNIT_TARGET_BASIC == 0 then return nil end
	local enemy = team & TEAM.DOTA_UNIT_TARGET_TEAM_ENEMY ~= 0
	local friend = team & TEAM.DOTA_UNIT_TARGET_TEAM_FRIENDLY ~= 0
	local custom = team & TEAM.DOTA_UNIT_TARGET_TEAM_CUSTOM ~= 0
	local side = Enum.TeamType.TEAM_BOTH
	if enemy and not friend then
		side = Enum.TeamType.TEAM_ENEMY
	elseif friend and not enemy and not custom then
		side = Enum.TeamType.TEAM_FRIEND
	end
	local my_team = Entity.GetTeamNum(hero)
	local target
	if ui.target:Get() == 0 and heroes then
		target = Input.GetNearestHeroToCursor(my_team, side)
	else
		target = Input.GetNearestUnitToCursor(my_team, side)
	end
	if not target then return nil end
	local cursor = Input.GetWorldCursorPos()
	if cursor:Distance2D(Entity.GetAbsOrigin(target)) > ui.radius:Get() then return nil end
	return target
end

local function cast(job, ability, self_cast)
	local hero = state.hero
	local behavior = Ability.GetBehavior(ability)
	local unit = has(behavior, BEHAVIOR.DOTA_ABILITY_BEHAVIOR_UNIT_TARGET)
	local point = has(behavior, BEHAVIOR.DOTA_ABILITY_BEHAVIOR_POINT)
	local none = has(behavior, BEHAVIOR.DOTA_ABILITY_BEHAVIOR_NO_TARGET)
	log(string.format("cast %s behavior=%s unit=%s point=%s none=%s self=%s", Ability.GetName(ability), tostring(behavior),
		tostring(unit), tostring(point), tostring(none), tostring(self_cast)))
	if has(behavior, BEHAVIOR.DOTA_ABILITY_BEHAVIOR_PASSIVE) then return false, L("jv_r_passive") end
	if has(behavior, BEHAVIOR.DOTA_ABILITY_BEHAVIOR_TOGGLE) then
		mark_order(job)
		Ability.Toggle(ability, false, false, true, "jarvis")
		return true, L("jv_r_cast_now")
	end
	if none then
		mark_order(job)
		Ability.CastNoTarget(ability, false, false, true, "jarvis")
		return true, L("jv_r_cast_now")
	end
	if self_cast then
		mark_order(job)
		if unit then
			Ability.CastTarget(ability, hero, false, false, true, "jarvis")
			return true, L("jv_r_cast_self")
		end
		return "js", L("jv_r_cast_self"), run_js(K.JS_SELF, ability)
	end
	if instant_for(ability) then
		if unit then
			local target = find_target(ability)
			if target then
				mark_order(job)
				Ability.CastTarget(ability, target, false, false, true, "jarvis")
				return true, string.format(L("jv_r_cast_target"), NPC.GetUnitName(target))
			end
		end
		if point then
			mark_order(job)
			Ability.CastPosition(ability, Input.GetWorldCursorPos(), false, false, true, "jarvis")
			return true, L("jv_r_cast_cursor")
		end
		if unit and ui.no_target:Get() == 1 then return false, L("jv_r_no_target") end
	end
	mark_order(job)
	return "js", L("jv_r_cast_aim"), run_js(K.JS_AIM, ability)
end

local function do_cast(job, ability, self_cast, now)
	local result, message, tag = cast(job, ability, self_cast)
	if result == "js" then
		job.stage = "js"
		job.js_tag = tag
		job.js_at = now
		job.message = message
		return false
	end
	finish(job, result, message)
	return true
end

local function cooldown_blocks(job, ability)
	if not ui.cd_check:Get() then return false end
	local cd = Ability.GetCooldown(ability) or 0
	if cd <= K.CD_TOLERANCE then return false end
	finish(job, false, string.format(L("jv_r_cd"), cd))
	return true
end

local function find_item(names)
	local hero = state.hero
	local wanted = {}
	for _, name in ipairs(names) do wanted[name] = true end
	for _, slot in ipairs(K.SEARCH) do
		local item = NPC.GetItemByIndex(hero, slot)
		if item and wanted[Ability.GetName(item)] then return item, slot end
	end
	return nil
end

local function step_spell(job, now)
	local cmd = job.cmd
	local spell = K.SPELLS[cmd.spell]
	local ability = spell and state.ab.spells[cmd.spell]
	if not ability then
		finish(job, false, L("jv_r_no_ability"))
		return true
	end
	local stage = job.stage
	if stage == "start" then
		if not Ability.IsHidden(ability) then
			if cmd.invoke_only then
				finish(job, true, L("jv_r_in_slot"))
				return true
			end
			if cooldown_blocks(job, ability) then return true end
			return do_cast(job, ability, cmd.self_cast, now)
		end
		local orb = unlearned_orb(spell.orbs)
		if orb then
			finish(job, false, string.format(L("jv_r_orb"), orb))
			return true
		end
		if cooldown_blocks(job, ability) then return true end
		local invoke_cd = Ability.GetCooldown(state.ab.invoke) or 0
		if invoke_cd > 0.05 then
			if invoke_cd * 1000 <= ui.invoke_wait:Get() then
				job.stage = "wait_invoke"
				job.deadline = now + invoke_cd + 0.5
				log(string.format("waiting for Invoke %.2fs", invoke_cd))
				return false
			end
			if ui.cd_check:Get() then
				finish(job, false, string.format(L("jv_r_invoke_cd"), invoke_cd))
				return true
			end
		end
		job.stage = "orbs"
		job.tries = 0
		return step_spell(job, now)
	end
	if stage == "wait_invoke" then
		if (Ability.GetCooldown(state.ab.invoke) or 0) <= 0.02 then
			job.stage = "orbs"
			job.tries = 0
			return step_spell(job, now)
		end
		if now > job.deadline then
			finish(job, false, L("jv_r_invoke_late"))
			return true
		end
		return false
	end
	if stage == "orbs" then
		mark_order(job)
		job.tries = job.tries + 1
		job.orbs_at = now
		job.stage = "invoke"
		if orbs_ready(spell.orbs) then return step_spell(job, now) end
		press_orbs(spell.orbs)
		return false
	end
	if stage == "invoke" then
		local ready = orbs_ready(spell.orbs)
		local waited = now - job.orbs_at
		if not ready and waited < (ready == nil and K.ORB_BLIND or K.ORB_WAIT) then return false end
		if ready == false then
			if job.tries <= ui.retries:Get() then
				job.stage = "orbs"
				log("orbs did not match, pressing them again")
				return step_spell(job, now)
			end
			finish(job, false, L("jv_r_invoke_fail"))
			return true
		end
		if not ready then log_modifiers() end
		order_no_target(state.ab.invoke)
		job.stage = "confirm"
		job.deadline = now + ui.confirm:Get() / 1000
		log(string.format("invoke %s (%s) try %d, orbs %s after %d ms", cmd.spell, spell.orbs, job.tries,
			ready and "set" or "not seen", floor(waited * 1000)))
		return false
	end
	if stage == "confirm" then
		if not Ability.IsHidden(ability) then
			log(string.format("invoke confirmed in %d ms", floor((now - job.deadline) * 1000 + ui.confirm:Get())))
			if cmd.invoke_only then
				finish(job, true, L("jv_r_invoked"))
				return true
			end
			local stance = current_stance()
			local done = do_cast(job, ability, cmd.self_cast, now)
			if stance and ui.restore:Get() and not same_orbs(stance, spell.orbs) then
				state.deferred = { ability = ability, orbs = stance, deadline = now + K.DEFERRED_TIME }
			end
			return done
		end
		if now > job.deadline then
			if job.tries <= ui.retries:Get() then
				job.stage = "orbs"
				log("invoke was not confirmed, retry")
				return step_spell(job, now)
			end
			finish(job, false, L("jv_r_invoke_fail"))
			return true
		end
		return false
	end
	finish(job, false, L("jv_r_unknown"))
	return true
end

local function step_item(job, now)
	local cmd = job.cmd
	local item, slot = find_item(cmd.names or {})
	if not item then
		finish(job, false, L("jv_r_no_item"))
		return true
	end
	if slot >= 9 and slot <= 14 then
		finish(job, false, L("jv_r_stash"))
		return true
	end
	if slot >= 6 and slot <= 8 then
		finish(job, false, L("jv_r_backpack"))
		return true
	end
	if cooldown_blocks(job, item) then return true end
	return do_cast(job, item, cmd.self_cast, now)
end

local function step_press(job, now)
	local cmd = job.cmd
	local bind = cmd.bind
	local hero = state.hero
	local orb = K.ORB_KEYS[bind]
	if orb or bind == "invoke" then
		mark_order(job)
		order_no_target(orb and state.ab[orb] or state.ab.invoke)
		finish(job, true, L("jv_r_cast_now"))
		return true
	end
	if (bind == "stop" or bind == "hold") and state.combo then stop_combo(L("jv_r_combo_stop")) end
	if bind == "stop" then
		mark_order(job)
		Player.PrepareUnitOrders(Players.GetLocal(), ORDER.DOTA_UNIT_ORDER_STOP, nil, Vector(0, 0, 0), nil, ISSUER, hero,
			false, false, false, true, "jarvis")
		finish(job, true, L("jv_r_done"))
		return true
	end
	if bind == "hold" then
		mark_order(job)
		Player.HoldPosition(Players.GetLocal(), hero, false, false, true, "jarvis")
		finish(job, true, L("jv_r_done"))
		return true
	end
	local ability
	if bind == "slot_d" or bind == "slot_f" then
		local slot = invoked_slots()[bind == "slot_d" and 1 or 2]
		ability = slot and slot.ability
	elseif K.SLOTS[bind] then
		ability = NPC.GetItemByIndex(hero, K.SLOTS[bind])
	end
	if not ability then
		finish(job, false, L("jv_r_empty"))
		return true
	end
	if cooldown_blocks(job, ability) then return true end
	return do_cast(job, ability, cmd.double, now)
end

local function step_stance(job)
	local orbs = job.cmd.orbs or ""
	if #orbs ~= 3 or orbs:find("[^QWE]") then
		finish(job, false, L("jv_r_unknown"))
		return true
	end
	local orb = unlearned_orb(orbs)
	if orb then
		finish(job, false, string.format(L("jv_r_orb"), orb))
		return true
	end
	mark_order(job)
	press_orbs(orbs)
	state.stance = orbs
	finish(job, true, string.format(L("jv_r_orbs"), orbs))
	return true
end

local function step_learn(job)
	local orb = K.ORB_KEYS[job.cmd.bind]
	local ability = orb and state.ab[orb]
	if not ability then
		finish(job, false, L("jv_r_no_ability"))
		return true
	end
	if Hero.GetAbilityPoints(state.hero) <= 0 then
		finish(job, false, L("jv_r_no_points"))
		return true
	end
	mark_order(job)
	Player.PrepareUnitOrders(Players.GetLocal(), ORDER.DOTA_UNIT_ORDER_TRAIN_ABILITY, nil, Vector(0, 0, 0), ability, ISSUER,
		state.hero, false, false, false, true, "jarvis")
	finish(job, true, L("jv_r_learned"))
	return true
end

local function step_combo(job, now)
	if job.stage == "combo" then
		local combo = state.combo
		if not combo then
			finish(job, false, L("jv_r_combo_stop"))
			return true
		end
		if combo_down(combo) then
			combo.seen = true
			combo.down_at = now
			log(string.format("combo key is down after %d ms", floor((now - combo.at) * 1000)))
			finish(job, true, L("jv_r_combo_on"))
			return true
		end
		if now > job.deadline then
			state.combo = nil
			send_keys({ { op = "up" } })
			finish(job, false, L("jv_r_combo_fail"))
			return true
		end
		return false
	end
	local slot = math.tointeger(job.cmd.slot) or -1
	local key_bind = invoker_widget("Main Settings", "Hero Settings", "Combo Key")
	local slot_bind = invoker_widget("Binds", "Switch Slot", "Slot #" .. (slot == 0 and "D" or slot))
	local count = combo_count()
	log(string.format("combo slot=%d menu: key=%s slot=%s count=%d", slot, tostring(key_bind ~= nil),
		tostring(slot_bind ~= nil), count))
	if not key_bind then
		finish(job, false, L("jv_r_combo_menu"))
		return true
	end
	if slot < 0 or slot > count then
		finish(job, false, string.format(L("jv_r_combo_mode"), count))
		return true
	end
	if not slot_bind then
		finish(job, false, L("jv_r_combo_slot"))
		return true
	end
	local keys, bad = bind_keys(key_bind)
	local slot_keys, bad_slot = bind_keys(slot_bind)
	if not keys or not slot_keys then
		finish(job, false, string.format(L("jv_r_combo_key"), bad or bad_slot))
		return true
	end
	if #keys == 0 then
		finish(job, false, L("jv_r_combo_nokey"))
		return true
	end
	if #slot_keys == 0 then
		local found, code = pcall(function() return Enum.ButtonCode["KEY_F" .. (13 + slot)] end)
		if found and code and pcall(function() slot_bind:Set(code) end) then
			slot_keys = bind_keys(slot_bind) or {}
			log(string.format("slot bind was empty, set to F%d: %s", 13 + slot, table.concat(slot_keys, "+")))
		end
		if #slot_keys == 0 then
			finish(job, false, L("jv_r_combo_bind"))
			return true
		end
	end
	local rmb = true
	for _, name in ipairs(keys) do
		if name == "KEY_MOUSE2" then rmb = false end
	end
	send_keys({ { op = "up" }, { op = "tap", keys = slot_keys }, { op = "down", keys = keys } })
	mark_order(job)
	state.combo = { label = job.label, bind = key_bind, at = now, down_at = now, rmb = rmb, seen = false }
	job.stage = "combo"
	job.deadline = now + K.COMBO_CONFIRM
	log(string.format("combo %d: tap %s, hold %s", slot, table.concat(slot_keys, "+"), table.concat(keys, "+")))
	return false
end

local function step(job, now)
	if job.stage == "js" then
		if now - job.js_at < K.JS_WAIT then return false end
		local ok, answer = js_result(job.js_tag)
		log("js " .. tostring(job.js_tag) .. " -> " .. tostring(answer))
		if ok then
			finish(job, true, job.message)
		else
			finish(job, false, string.format(L("jv_r_js"), answer))
		end
		return true
	end
	local kind = job.cmd.kind
	if kind ~= "learn" and not Entity.IsAlive(state.hero) then
		finish(job, false, L("jv_r_dead"))
		return true
	end
	if kind == "spell" then return step_spell(job, now) end
	if kind == "item" then return step_item(job, now) end
	if kind == "press" then return step_press(job, now) end
	if kind == "stance" then return step_stance(job) end
	if kind == "learn" then return step_learn(job) end
	if kind == "combo" then
		local ok, done = pcall(step_combo, job, now)
		if ok then return done end
		Log.Write(K.TAG .. "combo: " .. tostring(done))
		if state.job == job then finish(job, false, tostring(done)) end
		return true
	end
	finish(job, false, L("jv_r_unknown"))
	return true
end

local function run_deferred(now)
	local deferred = state.deferred
	if not deferred then return end
	if now > deferred.deadline then
		state.deferred = nil
		return
	end
	if (Ability.GetCooldown(deferred.ability) or 0) <= 0 then return end
	state.deferred = nil
	if not Entity.IsAlive(state.hero) or unlearned_orb(deferred.orbs) then return end
	press_orbs(deferred.orbs)
	report(string.format(L("jv_r_orbs"), deferred.orbs), true, L("jv_r_done"), nil, true)
end

local function run_jobs(now)
	for _ = 1, 4 do
		local job = state.job
		if not job then
			if now < state.next_job_at then return end
			job = table.remove(state.queue, 1)
			if not job then
				run_deferred(now)
				return
			end
			state.job = job
			state.deferred = nil
			job.stage = "start"
			local age = (now - job.recv_at) * 1000 + job.age_ms
			if age > ui.max_age:Get() then
				finish(job, false, string.format(L("jv_r_old"), floor(age)))
				job = nil
			end
		end
		if job and not step(job, now) then return end
	end
end

local function refresh_context()
	state.in_game = Engine.IsInGame()
	local hero = state.in_game and Heroes.GetLocal() or nil
	if hero and NPC.GetUnitName(hero) ~= K.HERO then hero = nil end
	local index = hero and Entity.GetIndex(hero) or nil
	if index ~= state.hero_index then
		state.hero_index = index
		state.ab = nil
		state.stance = nil
		clear_jobs()
	end
	state.hero = hero
	if hero and not state.ab then state.ab = load_abilities(hero) end
	state.ready = hero ~= nil and state.ab ~= nil
end

local function update()
	refresh_context()
	local now = os.clock()
	if not linked() then
		if state.link.online then set_online(false, "script is off") end
		if state.job or #state.queue > 0 or state.combo then clear_jobs() end
		combo_guard(now)
		return
	end
	if not Input.IsInputCaptured() then
		if ui.wake_key:IsPressed() then send_action("wake") end
		if ui.mute_key:IsPressed() then send_action("toggle_mute") end
	end
	combo_guard(now)
	poll(now)
	if state.ready then run_jobs(now) end
end

local function mix(a, b, t)
	return a + (b - a) * t
end

local function mix_color(into, target, t)
	for i = 1, #target do into[i] = mix(into[i], target[i], t) end
end

local function copy_palette(source)
	local out = { blobs = {}, core = { table.unpack(source.core) }, halo = { table.unpack(source.halo) } }
	for i, blob in ipairs(source.blobs) do out.blobs[i] = { table.unpack(blob) } end
	return out
end

local function orb_step(dt, look, level)
	local m = state.orb
	m.time = m.time + dt
	local k = 1 - exp(-dt * 6)
	if look.palette then
		local target = K.PALETTES[look.palette]
		if not m.palette then m.palette = copy_palette(target) end
		mix_color(m.palette.core, target.core, k)
		mix_color(m.palette.halo, target.halo, k)
		for i, blob in ipairs(target.blobs) do mix_color(m.palette.blobs[i], blob, k) end
	end
	m.velocity = m.velocity + (140 * (look.scale - m.scale) - 17 * m.velocity) * dt
	m.scale = m.scale + m.velocity * dt
	local fade = look.opacity > m.opacity and 7 or 4
	m.opacity = m.opacity + (look.opacity - m.opacity) * (1 - exp(-dt * fade))
	m.speed = m.speed + (look.speed - m.speed) * k
	m.phase = m.phase + dt * m.speed
	m.arc = m.arc + (look.arc - m.arc) * k
	local target
	if look.voice then
		target = clamp((level - 0.3) / 0.5, 0, 1)
	else
		target = look.breath * (1 + sin(m.time * look.rate))
	end
	local rate = target > m.amp and 18 or 5
	m.amp = m.amp + (target - m.amp) * (1 - exp(-dt * rate))
end

local function rgba(c, alpha)
	return Color(floor(c[1] + 0.5), floor(c[2] + 0.5), floor(c[3] + 0.5), floor(clamp(alpha, 0, 1) * 255 + 0.5))
end

local function orb_draw(cx, cy, radius, alpha)
	local m = state.orb
	local palette = m.palette
	local r = radius * m.scale * (1 + m.amp * 0.13)
	local o = clamp(m.opacity, 0, 1) * alpha
	if not palette or o <= 0.004 or r <= 0.5 then return end
	local amp = m.amp
	local center = Vec2(cx, cy)
	Render.CircleGradient(center, r * K.HALO, rgba(palette.halo, 0), rgba(palette.halo, (0.3 + 0.4 * amp) * o))
	Render.FilledCircle(center, r, rgba(palette.core, o), 0, 1, 64)
	for i, blob in ipairs(palette.blobs) do
		local n = i - 1
		local angle = m.phase * (n % 2 == 1 and -1 or 1) + n * 2.1
		local distance = r * (0.38 + 0.15 * sin(m.phase * 2.2 + n))
		local spot = Vec2(cx + cos(angle) * distance, cy + sin(angle) * distance)
		Render.CircleGradient(spot, r - distance, rgba(blob, 0), rgba(blob, blob[4] * (0.85 + 0.15 * amp) * o))
	end
	local shine_x, shine_y = cx - r * 0.3, cy - r * 0.34
	Render.CircleGradient(Vec2(shine_x, shine_y), r * 0.54, Color(255, 255, 255, 0), Color(255, 255, 255, floor(0.32 * o * 255)))
	Render.Circle(center, r, Color(255, 255, 255, floor((0.18 + 0.3 * amp) * o * 255)), 1, 0, 1, false, 64)
	if m.arc > 0.01 then
		Render.Circle(center, r * 0.95, Color(255, 255, 255, floor(0.6 * m.arc * o * 255)), 1.5, (m.time * 172) % 360, 0.207, true, 48)
	end
end

local function orb_look(opened)
	local link = state.link
	if not ui.orb:Get() then return "hidden" end
	if ui.orb_match:Get() and not state.hero then return "hidden" end
	if not link.online then return ui.orb_offline:Get() and "offline" or "sleeping" end
	local activity = link.activity
	if activity == "listening" and not state.ready then return "blocked" end
	if activity == "sleeping" and opened then return "preview" end
	return K.LOOKS[activity] and activity or "sleeping"
end

local function status_text()
	local link = state.link
	if not JSON then return L("jv_st_nojson") end
	if not link.online then return L("jv_st_offline") end
	local activity = link.activity
	if activity == "loading" then return L("jv_st_loading") end
	if activity == "error" then return string.format(L("jv_st_error"), link.error) end
	if activity == "muted" then return L("jv_st_muted") end
	if activity == "listening" then return L("jv_st_listening") end
	return string.format(L("jv_st_sleeping"), link.wake ~= "" and link.wake or "Jarvis")
end

local function orb_place(screen, size)
	local m = state.orb
	if not m.x then
		m.x = Config.ReadInt(K.CFG, "orb_x", -1)
		m.y = Config.ReadInt(K.CFG, "orb_y", -1)
		if m.x < 0 or m.y < 0 then
			m.x = floor(screen.x - 140)
			m.y = floor(screen.y * 0.3)
		end
	end
	local half = size / 2
	m.x = clamp(m.x, half, max(half, screen.x - half))
	m.y = clamp(m.y, half, max(half, screen.y - half))
end

local function orb_drag(size)
	local m = state.orb
	local down = Menu.Opened() and Input.IsKeyDown(Enum.ButtonCode.KEY_MOUSE1, true)
	local mx, my = Input.GetCursorPos()
	if down and not m.mouse then
		local dx, dy = mx - m.x, my - m.y
		if dx * dx + dy * dy <= (size / 2) * (size / 2) then m.drag = { dx = dx, dy = dy } end
	end
	if m.drag then
		if down then
			m.x, m.y = mx - m.drag.dx, my - m.drag.dy
		else
			m.drag = nil
			Config.WriteInt(K.CFG, "orb_x", floor(m.x))
			Config.WriteInt(K.CFG, "orb_y", floor(m.y))
		end
	end
	m.mouse = down
end

local function centered_text(text, cx, y, screen, color)
	local m = state.orb
	if not m.font then m.font = Render.LoadFont("Inter", Enum.FontCreate.FONTFLAG_ANTIALIAS, Enum.FontWeight.BOLD) end
	local font = m.font
	local width = Render.TextSize(font, 12, text).x
	local x = clamp(cx - width / 2, 4, max(4, screen.x - width - 4))
	Render.Text(font, 12, text, Vec2(floor(x), floor(y)), color)
end

local function draw_orb(now, dt, screen)
	local m = state.orb
	local opened = Menu.Opened()
	local look = orb_look(opened)
	if look ~= m.look then
		m.look = look
		log(string.format("orb %s: online=%s in_game=%s hero=%s menu=%s offline_dot=%s only_match=%s", look,
			tostring(state.link.online), tostring(state.in_game), tostring(state.hero ~= nil), tostring(opened),
			tostring(ui.orb_offline:Get()), tostring(ui.orb_match:Get())))
	end
	if look == "hidden" then
		m.drag = nil
		m.opacity = 0
		return
	end
	local shown = look ~= "sleeping"
	local size = ui.orb_size:Get()
	orb_place(screen, size)
	if shown then
		orb_drag(size)
	else
		m.drag = nil
	end
	orb_step(dt, K.LOOKS[look], state.link.level)
	local alpha = ui.orb_alpha:Get() / 100
	orb_draw(m.x, m.y, size / 2, alpha)
	local y = m.y + size / 2 + 8
	if opened and shown then
		centered_text(status_text(), m.x, y, screen, Color(240, 240, 242, 230))
		y = y + 18
	end
	local caption = state.caption
	if caption and ui.caption:Get() and state.in_game then
		local left = K.CAPTION_TIME - (now - caption.at)
		if left > 0 then
			local a = floor(clamp(left / 0.4, 0, 1) * 235)
			centered_text(caption.text, m.x, y, screen, caption.ok and Color(240, 240, 242, a) or Color(232, 98, 90, a))
		else
			state.caption = nil
		end
	end
end

local function draw()
	local m = state.orb
	local now = os.clock()
	local dt = m.last and clamp(now - m.last, 0, 0.05) or 0
	m.last = now
	if not enabled() then return end
	if state.in_game and not state.hero then return end
	local screen = Render.ScreenSize()
	draw_orb(now, dt, screen)
end

local function guarded(name, fn)
	local ok, err = pcall(fn)
	if not ok and state.last_error ~= err then
		state.last_error = err
		Log.Write(K.TAG .. name .. ": " .. tostring(err))
	end
end

local script = {}

function script.OnUpdateEx()
	guarded("update", update)
end

function script.OnFrame()
	guarded("draw", draw)
end

function script.OnGameEnd()
	state.hero = nil
	state.hero_index = nil
	state.ab = nil
	state.stance = nil
	state.js.hud = nil
	state.caption = nil
	state.mods_logged = false
	clear_jobs()
end

return script
