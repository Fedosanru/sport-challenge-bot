from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove)

from db import Challenge, Database, Exercise, Group

BOT_TOKEN=os.getenv('BOT_TOKEN','')
DATABASE_PATH=os.getenv('DATABASE_PATH','sport_challenge.db')
TIMEZONE=ZoneInfo(os.getenv('TIMEZONE','Europe/Moscow'))
ADMIN_IDS={int(x) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().isdigit()}
if not BOT_TOKEN: raise RuntimeError('BOT_TOKEN is not set')

db=Database(DATABASE_PATH); router=Router()

class ResultForm(StatesGroup): collecting=State(); confirm=State()
class ChallengeForm(StatesGroup):
    title=State(); start_date=State(); duration=State(); exercise_name=State(); exercise_unit=State(); exercise_target=State(); exercise_points=State(); exercise_more=State(); rule_mode=State(); over_target=State(); success_mode=State(); min_points=State(); edit_days=State(); join_mode=State()
class CloneForm(StatesGroup): choose=State(); title=State(); start_date=State(); duration=State()

def now(): return datetime.now(TIMEZONE)
def private(m:Message): return m.chat.type==ChatType.PRIVATE

def main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text='📝 Внести результат'),KeyboardButton(text='📊 Моя статистика')],
        [KeyboardButton(text='🏆 Общий рейтинг'),KeyboardButton(text='📊 Статистика челленджа')],
        [KeyboardButton(text='🏠 Выбрать группу'),KeyboardButton(text='📋 Правила')],
        [KeyboardButton(text='🔔 Напоминания'),KeyboardButton(text='🛠 Управление группой')]],resize_keyboard=True)

def manage_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text='🟢 Активные челленджи')],
        [KeyboardButton(text='➕ Новый челлендж'),KeyboardButton(text='📋 Копировать челлендж')],
        [KeyboardButton(text='🗂 Архив челленджей'),KeyboardButton(text='🏁 Завершить челлендж')],
        [KeyboardButton(text='⬅️ Главное меню')]],resize_keyboard=True)

def group_keyboard(bot_username:str,group_id:int,view='rating'):
    switch=('📊 Статистика',f'public_stats:{group_id}') if view=='rating' else ('🏆 Рейтинг',f'public_rating:{group_id}')
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📝 Внести результат',url=f'https://t.me/{bot_username}?start=result_{group_id}')],
        [InlineKeyboardButton(text=switch[0],callback_data=switch[1]),InlineKeyboardButton(text='📋 Правила',callback_data=f'public_rules:{group_id}')]])

def group_choice(rows,prefix='group'):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=('✅ ' if r['has_active'] else '')+r['title'],callback_data=f'{prefix}:{r["id"]}')] for r in rows])

def result_challenge_choice(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f'🏠 {r["group_title"]} · {r["challenge_title"]}',
            callback_data=f'result_group:{r["group_id"]}',
        )
    ] for r in rows])

def result_date_keyboard(edit_days:int,has_yesterday=False):
    today=now().date(); rows=[]
    for offset in range(0,edit_days+1):
        d=today-timedelta(days=offset); label='Сегодня' if offset==0 else 'Вчера' if offset==1 else d.strftime('%d.%m')
        rows.append([InlineKeyboardButton(text=label,callback_data=f'date:{d.isoformat()}')])
    if has_yesterday: rows.append([InlineKeyboardButton(text='⚡ Повторить вчера',callback_data='repeat:yesterday')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Сохранить',callback_data='result:save'),InlineKeyboardButton(text='✏️ Заново',callback_data='result:edit')],[InlineKeyboardButton(text='❌ Отмена',callback_data='result:cancel')]])

def resume_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='▶️ Продолжить',callback_data='draft:resume'),InlineKeyboardButton(text='🔄 Начать заново',callback_data='draft:restart')],[InlineKeyboardButton(text='❌ Отмена',callback_data='result:cancel')]])

def saved_result_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✏️ Изменить результат',callback_data='saved:edit_today')],[InlineKeyboardButton(text='🏆 Рейтинг',callback_data='saved:rating'),InlineKeyboardButton(text='📊 Моя статистика',callback_data='saved:stats')]])

def admin_challenge_keyboard(challenge_id:int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🏆 Рейтинг',callback_data=f'admin_rating:{challenge_id}'), InlineKeyboardButton(text='📋 Правила',callback_data=f'admin_rules:{challenge_id}')],
        [InlineKeyboardButton(text='🔄 Обновить',callback_data=f'admin_active:{challenge_id}')],
        [InlineKeyboardButton(text='🏁 Завершить',callback_data=f'admin_finish:{challenge_id}')],
        [InlineKeyboardButton(text='⬅️ К списку',callback_data='admin_active_list')],
    ])

def finish_confirm_keyboard(challenge_id:int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='✅ Да, завершить',callback_data=f'admin_finish_confirm:{challenge_id}')],
        [InlineKeyboardButton(text='❌ Отмена',callback_data=f'admin_active:{challenge_id}')],
    ])

def fmt(v:float)->str: return str(int(v)) if float(v).is_integer() else f'{v:g}'

def selected(user_id:int)->tuple[Group|None,Challenge|None]:
    g=db.get_selected_group(user_id); return g,db.get_active_challenge(g.id) if g else None

async def ensure_group(message:Message)->Group:
    g=db.upsert_group(message.chat.id,message.chat.title or 'Группа')
    if message.from_user:
        db.upsert_user(message.from_user.id,message.from_user.full_name,message.from_user.username)
        role='admin' if (await message.bot.get_chat_member(message.chat.id,message.from_user.id)).status in {'administrator','creator'} else 'member'
        db.add_member(g.id,message.from_user.id,role)
    return g

def rules_text(ch:Challenge)->str:
    exercises=db.get_exercises(ch.id)
    scoring={
        'proportional':'Пропорционально выполнению нормы',
        'binary':'Только за полное выполнение нормы',
        'step':'Ступенчато: 50% нормы — половина баллов, 100% — все баллы',
        'fixed':'Фиксированный балл за любой результат выше нуля',
    }.get(ch.scoring_mode,ch.scoring_mode)
    over={'cap':'Сверх нормы не учитывается','stats_only':'Сверх нормы видно в статистике, но баллы ограничены','bonus':'Сверх нормы учитывается только в статистике'}.get(ch.over_target_mode,ch.over_target_mode)
    success={'any':'Внесён хотя бы один ненулевой результат','all_targets':'Выполнены нормы всех упражнений','min_points':f'Набрано не менее {fmt(ch.min_daily_points)} балла'}.get(ch.success_mode,ch.success_mode)
    join={'open':'Можно вступать после старта','before_start':'Вступление только до даты старта','manual':'Участников добавляет администратор'}.get(ch.join_mode,ch.join_mode)
    lines=[f'<b>📋 Правила: {escape(ch.title)}</b>',f'{date.fromisoformat(ch.start_date):%d.%m.%Y} — {date.fromisoformat(ch.end_date):%d.%m.%Y}','', '<b>Упражнения:</b>']
    for e in exercises: lines.append(f'• {escape(e.name)}: норма {fmt(e.daily_target)} {escape(e.unit)}, максимум {fmt(e.max_points)} балл.')
    lines += ['',f'<b>Начисление:</b> {escape(scoring)}',f'<b>Перевыполнение:</b> {escape(over)}',f'<b>Успешный день:</b> {escape(success)}',f'<b>Исправление:</b> сегодня и ещё {ch.edit_days} прошл. дн.',f'<b>Участие:</b> {escape(join)}',f'Максимум за день: <b>{fmt(sum(e.max_points for e in exercises))}</b> балл.','', '<i>После запуска правила зафиксированы и не меняются задним числом.</i>']
    return '\n'.join(lines)

def ranking_text(ch:Challenge)->str:
    rows=db.get_ranking(ch.id); stats=db.get_group_stats(ch.id,now().date().isoformat())
    start=date.fromisoformat(ch.start_date); end=date.fromisoformat(ch.end_date); today=now().date(); day=max(1,min((today-start).days+1,(end-start).days+1)); total_days=(end-start).days+1
    lines=[f'<b>🏆 {escape(ch.title)}</b>',f'📅 День {day} из {total_days} · 👥 {stats["members"]} участников','']
    medals=['🥇','🥈','🥉']
    if not rows: lines.append('Пока никто не внёс результат.')
    for i,r in enumerate(rows[:15],1): lines.append(f'{medals[i-1] if i<=3 else str(i)+"."} {escape(r["full_name"])} — <b>{float(r["points"]):.2f}</b>')
    lines += ['',f'✅ Сегодня отметились: <b>{stats["active_today"]} / {stats["members"]}</b>',f'⏳ До конца: <b>{max((end-today).days,0)}</b> дн.']
    totals=db.get_totals_by_exercise(ch.id,today.isoformat())
    if totals:
        lines.append('\n<b>Сегодня вместе:</b>')
        lines.extend(f'• {escape(r["name"])}: {fmt(float(r["total"]))} {escape(r["unit"])}' for r in totals)
    return '\n'.join(lines)

def group_stats_text(ch:Challenge)->str:
    s=db.get_group_stats(ch.id,now().date().isoformat()); rows=db.get_ranking(ch.id); totals=db.get_totals_by_exercise(ch.id)
    lines=[f'<b>📊 {escape(ch.title)}</b>','',f'👥 Участников: <b>{s["members"]}</b>',f'✅ Сегодня: <b>{s["active_today"]}</b>',f'📝 Дней с результатами: <b>{s["result_days"]}</b>',f'⭐ Всего баллов: <b>{float(s["points"]):.2f}</b>']
    if rows: lines.append(f'🏆 Лидер: <b>{escape(rows[0]["full_name"])}</b>')
    lines.append('\n<b>Итоги по упражнениям:</b>')
    lines.extend(f'• {escape(r["name"])}: {fmt(float(r["total"]))} {escape(r["unit"])}' for r in totals)
    return '\n'.join(lines)

def admin_active_text(ch:Challenge)->str:
    g=db.get_group(ch.group_id)
    today=now().date(); start=date.fromisoformat(ch.start_date); end=date.fromisoformat(ch.end_date)
    stats=db.get_group_stats(ch.id,today.isoformat()); ranking=db.get_ranking(ch.id)
    if today < start: phase=f'Старт через {(start-today).days} дн.'
    elif today > end: phase=f'Срок завершён {(today-end).days} дн. назад'
    else: phase=f'День {(today-start).days+1} из {(end-start).days+1}'
    lines=[f'<b>🟢 {escape(ch.title)}</b>',f'Группа: <b>{escape(g.title if g else "Неизвестная группа")}</b>',f'📅 {start:%d.%m.%Y} — {end:%d.%m.%Y}',f'⏱ {phase}','',f'👥 Участников: <b>{stats["members"]}</b>',f'✅ Отметились сегодня: <b>{stats["active_today"]}</b>',f'📝 Дней с результатами: <b>{stats["result_days"]}</b>',f'⭐ Начислено баллов: <b>{float(stats["points"]):.2f}</b>']
    if ranking: lines.append(f'🏆 Лидер: <b>{escape(ranking[0]["full_name"])}</b> — {float(ranking[0]["points"]):.2f}')
    exercises=db.get_exercises(ch.id)
    if exercises:
        lines.append('\n<b>Упражнения:</b>')
        lines.extend(f'• {escape(e.name)} — {fmt(e.daily_target)} {escape(e.unit)}' for e in exercises)
    return '\n'.join(lines)

@router.message(CommandStart())
async def start(message:Message,state:FSMContext,bot:Bot):
    await state.clear()
    if not message.from_user:return
    db.upsert_user(message.from_user.id,message.from_user.full_name,message.from_user.username)
    if not private(message):
        g=await ensure_group(message); await message.answer('Бот подключён к группе.',reply_markup=group_keyboard((await bot.get_me()).username,g.id)); return
    arg=(message.text or '').split(maxsplit=1)
    if len(arg)>1 and arg[1].startswith('result_'):
        try: gid=int(arg[1].split('_',1)[1]); db.add_member(gid,message.from_user.id); db.set_selected_group(message.from_user.id,gid)
        except ValueError: pass
        await message.answer('Группа выбрана.',reply_markup=main_keyboard()); await result_start(message,state); return
    groups=db.get_user_groups(message.from_user.id)
    if not groups: await message.answer('Добавьте бота в Telegram-группу и отправьте там /start.',reply_markup=main_keyboard()); return
    if len(groups)==1: db.set_selected_group(message.from_user.id,groups[0]['id'])
    await message.answer('Главное меню.',reply_markup=main_keyboard())

@router.message(F.text=='🏠 Выбрать группу')
async def choose_group(message:Message):
    if private(message) and message.from_user:
        rows=db.get_user_groups(message.from_user.id); await message.answer('Выберите группу:',reply_markup=group_choice(rows) if rows else None)

@router.callback_query(F.data.startswith('group:'))
async def set_group(callback:CallbackQuery):
    gid=int(callback.data.split(':')[1]); db.set_selected_group(callback.from_user.id,gid); g=db.get_group(gid)
    await callback.answer('Группа выбрана'); await callback.message.answer(f'Выбрана группа: <b>{escape(g.title)}</b>',reply_markup=main_keyboard())

async def open_result_for_challenge(message:Message,state:FSMContext,ch:Challenge):
    today=now().date()
    if today < date.fromisoformat(ch.start_date):
        await message.answer(f'Челлендж начнётся <b>{date.fromisoformat(ch.start_date):%d.%m.%Y}</b>.')
        return
    if today > date.fromisoformat(ch.end_date):
        await message.answer('Срок этого челленджа уже завершён.')
        return

    draft=db.get_result_draft(message.from_user.id)
    if draft and int(draft['challenge_id'])==ch.id:
        await message.answer(
            f'У вас есть незавершённый ввод за <b>{date.fromisoformat(draft["result_date"]):%d.%m.%Y}</b>.',
            reply_markup=resume_keyboard(),
        )
        return
    if draft:
        db.delete_result_draft(message.from_user.id)

    yesterday=(today-timedelta(days=1)).isoformat()
    has=bool(db.get_result(ch.id,message.from_user.id,yesterday))
    await state.update_data(challenge_id=ch.id)
    await message.answer('За какой день внести результат?',reply_markup=result_date_keyboard(ch.edit_days,has))

@router.message(F.text=='📝 Внести результат')
async def result_start(message:Message,state:FSMContext):
    if not private(message) or not message.from_user:return
    user_id=message.from_user.id
    g,ch=selected(user_id)

    # The selected group can be stale or can have no active season. In that
    # case search all groups in which the user participates.
    if not ch:
        rows=db.get_user_active_challenges(user_id)
        if not rows:
            await message.answer('У вас нет доступных активных челленджей. Откройте сообщение бота в группе и нажмите «📝 Внести результат».')
            return
        if len(rows)>1:
            await message.answer('Выберите челлендж, в который хотите внести результат:',reply_markup=result_challenge_choice(rows))
            return
        db.set_selected_group(user_id,int(rows[0]['group_id']))
        ch=db.get_challenge(int(rows[0]['challenge_id']))

    await open_result_for_challenge(message,state,ch)

@router.callback_query(F.data.startswith('result_group:'))
async def select_result_group(callback:CallbackQuery,state:FSMContext):
    gid=int(callback.data.split(':',1)[1])
    rows=db.get_user_active_challenges(callback.from_user.id)
    allowed={int(r['group_id']):int(r['challenge_id']) for r in rows}
    challenge_id=allowed.get(gid)
    if not challenge_id:
        await callback.answer('Челлендж больше недоступен',show_alert=True)
        return
    db.set_selected_group(callback.from_user.id,gid)
    ch=db.get_challenge(challenge_id)
    await callback.answer('Челлендж выбран')
    await open_result_for_challenge(callback.message,state,ch)

async def begin_result_entry(target_message:Message,user_id:int,ch:Challenge,target:date,state:FSMContext,prefill:dict[int,float]|None=None):
    exercises=db.get_exercises(ch.id)
    if not exercises:
        await target_message.answer('У челленджа нет упражнений.')
        return
    values={str(k):float(v) for k,v in (prefill or {}).items()}
    await state.update_data(challenge_id=ch.id,result_date=target.isoformat(),exercise_index=0,values=values)
    await state.set_state(ResultForm.collecting)
    db.save_result_draft(user_id,ch.id,target.isoformat(),0,values,'collecting')
    ex=exercises[0]
    old=values.get(str(ex.id))
    suffix=f' Текущее значение: <b>{fmt(old)}</b>.' if old is not None else ''
    await target_message.answer(f'{escape(ex.name)} — сколько {escape(ex.unit)}? Норма: {fmt(ex.daily_target)}.{suffix}')

@router.callback_query(F.data.startswith('date:'))
async def result_date(callback:CallbackQuery,state:FSMContext):
    ch_id=(await state.get_data()).get('challenge_id'); ch=db.get_challenge(ch_id) if ch_id else None
    if not ch: await callback.answer('Начните заново',show_alert=True); return
    try: target=date.fromisoformat(callback.data.split(':',1)[1])
    except ValueError: return await callback.answer('Неверная дата',show_alert=True)
    if not (date.fromisoformat(ch.start_date)<=target<=date.fromisoformat(ch.end_date)): await callback.answer('Дата вне челленджа',show_alert=True); return
    existing=db.get_result(ch.id,callback.from_user.id,target.isoformat()) or {}
    await callback.answer()
    await begin_result_entry(callback.message,callback.from_user.id,ch,target,state,existing)

@router.callback_query(F.data=='draft:resume')
async def resume_draft(callback:CallbackQuery,state:FSMContext):
    draft=db.get_result_draft(callback.from_user.id)
    if not draft:
        await callback.answer('Черновик не найден',show_alert=True); return
    ch=db.get_challenge(int(draft['challenge_id']))
    if not ch:
        db.delete_result_draft(callback.from_user.id); await callback.answer('Челлендж не найден',show_alert=True); return
    values={str(k):float(v) for k,v in draft.get('values',{}).items()}
    idx=int(draft.get('exercise_index',0)); exercises=db.get_exercises(ch.id)
    await state.update_data(challenge_id=ch.id,result_date=draft['result_date'],exercise_index=idx,values=values)
    await callback.answer()
    if draft.get('stage')=='confirm' or idx>=len(exercises):
        await state.set_state(ResultForm.confirm)
        await callback.message.answer(confirm_text(ch,values),reply_markup=confirm_keyboard())
    else:
        await state.set_state(ResultForm.collecting)
        ex=exercises[idx]
        old=values.get(str(ex.id)); suffix=f' Текущее значение: <b>{fmt(old)}</b>.' if old is not None else ''
        await callback.message.answer(f'{escape(ex.name)} — сколько {escape(ex.unit)}? Норма: {fmt(ex.daily_target)}.{suffix}')

@router.callback_query(F.data=='draft:restart')
async def restart_draft(callback:CallbackQuery,state:FSMContext):
    draft=db.get_result_draft(callback.from_user.id)
    ch=db.get_challenge(int(draft['challenge_id'])) if draft else None
    if not ch:
        await callback.answer('Черновик не найден',show_alert=True); return
    db.delete_result_draft(callback.from_user.id)
    await callback.answer()
    await begin_result_entry(callback.message,callback.from_user.id,ch,date.fromisoformat(draft['result_date']),state,{})

@router.callback_query(F.data=='repeat:yesterday')
async def repeat_yesterday(callback:CallbackQuery,state:FSMContext):
    d=await state.get_data(); ch=db.get_challenge(d.get('challenge_id'))
    if not ch:return
    old=db.get_result(ch.id,callback.from_user.id,(now().date()-timedelta(days=1)).isoformat())
    if not old: await callback.answer('Нет результата за вчера',show_alert=True); return
    values={str(k):v for k,v in old.items()}
    await state.update_data(result_date=now().date().isoformat(),values=values,exercise_index=len(db.get_exercises(ch.id)))
    await state.set_state(ResultForm.confirm)
    db.save_result_draft(callback.from_user.id,ch.id,now().date().isoformat(),len(db.get_exercises(ch.id)),values,'confirm')
    await callback.answer(); await callback.message.answer(confirm_text(ch,old),reply_markup=confirm_keyboard())

def parse_value(message:Message,target:float)->tuple[float|None,str|None]:
    try: v=float((message.text or '').replace(',','.'))
    except ValueError:return None,'Введите число.'
    if v<0:return None,'Результат не может быть отрицательным.'
    safe_max=max(target*10,100000 if target<=0 else 0)
    if v>safe_max:return None,f'Слишком большое значение. Максимум для ввода: {fmt(safe_max)}.'
    return v,None

def confirm_text(ch:Challenge,values:dict[int|str,float])->str:
    exercises=db.get_exercises(ch.id); normalized={int(k):float(v) for k,v in values.items()}; lines=['<b>Проверка результата:</b>','']
    for e in exercises:
        v=normalized.get(e.id,0)
        lines.append(f'• {escape(e.name)}: <b>{fmt(v)}</b> {escape(e.unit)}')
    points=db.calculate_daily_score(ch.id,normalized)
    lines.append(f'\nБаллы: <b>{points:.2f}</b> из {fmt(sum(e.max_points for e in exercises))}')
    return '\n'.join(lines)

@router.message(ResultForm.collecting)
async def collect_result(message:Message,state:FSMContext):
    if not message.from_user:return
    d=await state.get_data(); ch=db.get_challenge(d.get('challenge_id'))
    if not ch:
        draft=db.get_result_draft(message.from_user.id)
        if not draft:return await message.answer('Форма устарела. Нажмите «📝 Внести результат».')
        ch=db.get_challenge(int(draft['challenge_id'])); d={'challenge_id':draft['challenge_id'],'result_date':draft['result_date'],'exercise_index':draft['exercise_index'],'values':draft['values']}
    exercises=db.get_exercises(ch.id); idx=int(d.get('exercise_index',0))
    if idx>=len(exercises):
        await state.set_state(ResultForm.confirm); return await message.answer(confirm_text(ch,d.get('values',{})),reply_markup=confirm_keyboard())
    ex=exercises[idx]; value,error=parse_value(message,ex.daily_target)
    if error: await message.answer(error); return
    values={str(k):v for k,v in d.get('values',{}).items()}; values[str(ex.id)]=value; idx+=1
    if idx<len(exercises):
        nxt=exercises[idx]
        await state.update_data(values=values,exercise_index=idx)
        db.save_result_draft(message.from_user.id,ch.id,d['result_date'],idx,values,'collecting')
        old=values.get(str(nxt.id)); suffix=f' Текущее значение: <b>{fmt(old)}</b>.' if old is not None else ''
        await message.answer(f'{escape(nxt.name)} — сколько {escape(nxt.unit)}? Норма: {fmt(nxt.daily_target)}.{suffix}')
    else:
        await state.update_data(values=values,exercise_index=idx); await state.set_state(ResultForm.confirm)
        db.save_result_draft(message.from_user.id,ch.id,d['result_date'],idx,values,'confirm')
        await message.answer(confirm_text(ch,values),reply_markup=confirm_keyboard())

@router.callback_query(F.data=='result:save')
async def save(callback:CallbackQuery,state:FSMContext):
    d=await state.get_data()
    if not {'challenge_id','result_date','values'}.issubset(d):
        draft=db.get_result_draft(callback.from_user.id)
        if draft:d={'challenge_id':draft['challenge_id'],'result_date':draft['result_date'],'values':draft['values']}
        else:return await callback.answer('Форма устарела. Внесите результат заново.',show_alert=True)
    try:
        values={int(k):float(v) for k,v in d['values'].items()}; ch=db.get_challenge(int(d['challenge_id']))
        if not ch:raise ValueError('Челлендж не найден')
        db.upsert_user(callback.from_user.id,callback.from_user.full_name,callback.from_user.username)
        db.save_result(ch.id,callback.from_user.id,d['result_date'],values)
        daily=db.calculate_daily_score(ch.id,values)
        db.delete_result_draft(callback.from_user.id); await state.clear(); await callback.answer('Сохранено')
        if callback.message:
            try:await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:pass
        exercise_lines='\n'.join(f'• {escape(e.name)}: <b>{fmt(values.get(e.id,0))}</b> {escape(e.unit)}' for e in db.get_exercises(ch.id))
        try:
            s=db.get_user_stats(ch.id,callback.from_user.id); place,total=db.get_user_rank(ch.id,callback.from_user.id); current,longest=db.calculate_streaks(ch.id,callback.from_user.id,now().date().isoformat())
            text=(f'✅ <b>Результат сохранён</b>\n\n{exercise_lines}\n\n🏅 За день: <b>{daily:.2f}</b>\n⭐ Всего: <b>{float(s["points"]):.2f}</b>\n🏆 Место: <b>{place or "—"} из {total}</b>\n🔥 Серия: <b>{current}</b> · рекорд: <b>{longest}</b>')
        except Exception:
            logging.exception('Result saved, but statistics calculation failed'); text=f'✅ <b>Результат сохранён</b>\n\n{exercise_lines}\n\n🏅 За день: <b>{daily:.2f}</b>'
        await callback.message.answer(text,reply_markup=saved_result_keyboard())
    except Exception as exc:
        logging.exception('Unable to save result'); await callback.answer('Не удалось сохранить результат',show_alert=True)
        if callback.message:await callback.message.answer(f'❌ Результат не сохранён. Ошибка: <code>{escape(str(exc))}</code>\nПопробуйте внести результат ещё раз.')

@router.callback_query(F.data=='result:edit')
async def edit(callback:CallbackQuery,state:FSMContext):
    d=await state.get_data()
    if not d:
        draft=db.get_result_draft(callback.from_user.id); d=draft or {}
    ch=db.get_challenge(int(d.get('challenge_id',0)))
    if not ch:return await callback.answer('Форма устарела',show_alert=True)
    existing=db.get_result(ch.id,callback.from_user.id,d.get('result_date',now().date().isoformat())) or {}
    await callback.answer(); await begin_result_entry(callback.message,callback.from_user.id,ch,date.fromisoformat(d.get('result_date',now().date().isoformat())),state,existing)

@router.callback_query(F.data=='saved:edit_today')
async def edit_saved_today(callback:CallbackQuery,state:FSMContext):
    g,ch=selected(callback.from_user.id)
    if not ch:return await callback.answer('Нет активного челленджа',show_alert=True)
    target=now().date(); existing=db.get_result(ch.id,callback.from_user.id,target.isoformat()) or {}
    await callback.answer(); await begin_result_entry(callback.message,callback.from_user.id,ch,target,state,existing)

@router.callback_query(F.data=='saved:rating')
async def saved_rating(callback:CallbackQuery):
    _,ch=selected(callback.from_user.id); await callback.answer()
    if callback.message:await callback.message.answer(ranking_text(ch) if ch else 'Нет активного челленджа.',reply_markup=main_keyboard())

@router.callback_query(F.data=='saved:stats')
async def saved_stats(callback:CallbackQuery):
    await callback.answer()
    g,ch=selected(callback.from_user.id)
    if not ch:
        if callback.message:await callback.message.answer('В выбранной группе нет активного челленджа.')
        return
    s=db.get_user_stats(ch.id,callback.from_user.id); place,total=db.get_user_rank(ch.id,callback.from_user.id); current,longest=db.calculate_streaks(ch.id,callback.from_user.id,now().date().isoformat()); totals=db.get_user_totals(ch.id,callback.from_user.id); days=int(s['days']); avg=float(s['points'])/days if days else 0
    lines=[f'<b>📊 {escape(g.title)}</b>',f'🏆 Место: <b>{place or "—"} из {total}</b>',f'⭐ Баллы: <b>{float(s["points"]):.2f}</b>',f'📅 Дней: <b>{days}</b>',f'🔥 Серия: <b>{current}</b> · рекорд: <b>{longest}</b>',f'📈 Среднее: <b>{avg:.2f}</b> балла/день','']
    lines.extend(f'• {escape(r["name"])}: {fmt(float(r["total"]))} {escape(r["unit"])}' for r in totals)
    if callback.message:await callback.message.answer('\n'.join(lines),reply_markup=main_keyboard())

@router.callback_query(F.data=='result:cancel')
async def cancel(callback:CallbackQuery,state:FSMContext):
    db.delete_result_draft(callback.from_user.id); await state.clear(); await callback.answer(); await callback.message.answer('Отменено.',reply_markup=main_keyboard())

@router.callback_query(F.data.startswith('public_rating:'))
async def public_rating(callback:CallbackQuery):
    gid=int(callback.data.split(':')[1]); g=db.get_group(gid); ch=db.get_active_challenge(gid); me=await callback.bot.get_me()
    if not g or not callback.message or callback.message.chat.id!=g.telegram_chat_id:return await callback.answer('Группа не найдена',show_alert=True)
    await callback.message.edit_text(ranking_text(ch) if ch else 'В этой группе нет активного челленджа.',reply_markup=group_keyboard(me.username,gid,'rating')); await callback.answer()

@router.callback_query(F.data.startswith('public_rules:'))
async def public_rules(callback:CallbackQuery):
    gid=int(callback.data.split(':')[1]); g=db.get_group(gid); ch=db.get_active_challenge(gid); me=await callback.bot.get_me()
    if not g or not callback.message or callback.message.chat.id!=g.telegram_chat_id:return await callback.answer('Группа не найдена',show_alert=True)
    await callback.message.edit_text(rules_text(ch) if ch else 'В этой группе нет активного челленджа.',reply_markup=group_keyboard(me.username,gid,'rating')); await callback.answer()

@router.callback_query(F.data.startswith('public_stats:'))
async def public_stats(callback:CallbackQuery):
    gid=int(callback.data.split(':')[1]); g=db.get_group(gid); ch=db.get_active_challenge(gid); me=await callback.bot.get_me()
    if not g or not callback.message or callback.message.chat.id!=g.telegram_chat_id:return await callback.answer('Группа не найдена',show_alert=True)
    await callback.message.edit_text(group_stats_text(ch) if ch else 'В этой группе нет активного челленджа.',reply_markup=group_keyboard(me.username,gid,'stats')); await callback.answer()

@router.message(F.text=='🏆 Общий рейтинг')
@router.message(Command('rating'))
async def rating(message:Message):
    if private(message):
        if not message.from_user:return
        _,ch=selected(message.from_user.id); await message.answer(ranking_text(ch) if ch else 'В этой группе нет активного челленджа.',reply_markup=main_keyboard())
    else:
        g=await ensure_group(message); ch=db.get_active_challenge(g.id); me=await message.bot.get_me(); await message.answer(ranking_text(ch) if ch else 'В этой группе нет активного челленджа.',reply_markup=group_keyboard(me.username,g.id))

@router.message(F.text=='📊 Статистика челленджа')
@router.message(Command('stats'))
async def stats(message:Message):
    if private(message):
        _,ch=selected(message.from_user.id); await message.answer(group_stats_text(ch) if ch else 'В этой группе нет активного челленджа.',reply_markup=main_keyboard())
    else:
        g=await ensure_group(message); ch=db.get_active_challenge(g.id); me=await message.bot.get_me(); await message.answer(group_stats_text(ch) if ch else 'В этой группе нет активного челленджа.',reply_markup=group_keyboard(me.username,g.id,'stats'))

@router.message(F.text=='📊 Моя статистика')
async def my_stats(message:Message):
    if not private(message) or not message.from_user:return
    g,ch=selected(message.from_user.id)
    if not ch:return await message.answer('В выбранной группе нет активного челленджа.')
    s=db.get_user_stats(ch.id,message.from_user.id); place,total=db.get_user_rank(ch.id,message.from_user.id); current,longest=db.calculate_streaks(ch.id,message.from_user.id,now().date().isoformat()); totals=db.get_user_totals(ch.id,message.from_user.id); days=int(s['days']); avg=float(s['points'])/days if days else 0
    lines=[f'<b>📊 {escape(g.title)}</b>',f'🏆 Место: <b>{place or "—"} из {total}</b>',f'⭐ Баллы: <b>{float(s["points"]):.2f}</b>',f'📅 Дней: <b>{days}</b>',f'🔥 Серия: <b>{current}</b> · рекорд: <b>{longest}</b>',f'📈 Среднее: <b>{avg:.2f}</b> балла/день','']
    lines.extend(f'• {escape(r["name"])}: {fmt(float(r["total"]))} {escape(r["unit"])}' for r in totals); await message.answer('\n'.join(lines))

@router.message(F.text=='📋 Правила')
async def rules(message:Message):
    if private(message) and message.from_user:
        _,ch=selected(message.from_user.id); await message.answer(rules_text(ch) if ch else 'Нет активного челленджа.')

@router.message(F.text=='🔔 Напоминания')
async def reminders(message:Message):
    if private(message) and message.from_user: await message.answer('🔔 Напоминания включены.' if db.toggle_reminders(message.from_user.id) else '🔕 Напоминания выключены.')

@router.message(F.text=='🛠 Управление группой')
async def manage(message:Message):
    if not private(message) or not message.from_user:return
    g=db.get_selected_group(message.from_user.id)
    if not g:return await message.answer('Сначала выберите группу.')
    if not (db.is_group_admin(g.id,message.from_user.id) or message.from_user.id in ADMIN_IDS):return await message.answer('Управлять группой может только администратор.')
    await message.answer(f'Управление группой «{escape(g.title)}»',reply_markup=manage_keyboard())

@router.message(F.text=='⬅️ Главное меню')
async def home(message:Message,state:FSMContext): await state.clear(); await message.answer('Главное меню.',reply_markup=main_keyboard())

@router.message(F.text=='🟢 Активные челленджи')
async def active_challenges(message:Message):
    if not private(message) or not message.from_user:return
    rows=db.get_admin_active_challenges(message.from_user.id,message.from_user.id in ADMIN_IDS)
    if not rows:return await message.answer('У вас нет активных челленджей.')
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'🟢 {r["group_title"]}: {r["title"]}',callback_data=f'admin_active:{r["id"]}')] for r in rows])
    await message.answer(f'<b>🟢 Активные челленджи</b>\n\nВсего: <b>{len(rows)}</b>',reply_markup=kb)

@router.callback_query(F.data=='admin_active_list')
async def active_challenges_callback(callback:CallbackQuery):
    rows=db.get_admin_active_challenges(callback.from_user.id,callback.from_user.id in ADMIN_IDS)
    if not rows:return await callback.answer('Активных челленджей нет',show_alert=True)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'🟢 {r["group_title"]}: {r["title"]}',callback_data=f'admin_active:{r["id"]}')] for r in rows])
    await callback.answer(); await callback.message.edit_text(f'<b>🟢 Активные челленджи</b>\n\nВсего: <b>{len(rows)}</b>',reply_markup=kb)

async def can_manage_challenge(user_id:int,ch:Challenge|None)->bool:
    return bool(ch and (user_id in ADMIN_IDS or db.is_group_admin(ch.group_id,user_id)))

@router.callback_query(F.data.startswith('admin_active:'))
async def active_challenge_card(callback:CallbackQuery):
    ch=db.get_challenge(int(callback.data.split(':')[1]))
    if not await can_manage_challenge(callback.from_user.id,ch):return await callback.answer('Недостаточно прав',show_alert=True)
    if ch.status!='active':return await callback.answer('Челлендж уже завершён',show_alert=True)
    await callback.answer(); await callback.message.edit_text(admin_active_text(ch),reply_markup=admin_challenge_keyboard(ch.id))

@router.callback_query(F.data.startswith('admin_rating:'))
async def admin_rating(callback:CallbackQuery):
    ch=db.get_challenge(int(callback.data.split(':')[1]))
    if not await can_manage_challenge(callback.from_user.id,ch):return await callback.answer('Недостаточно прав',show_alert=True)
    await callback.answer(); await callback.message.edit_text(ranking_text(ch),reply_markup=admin_challenge_keyboard(ch.id))

@router.callback_query(F.data.startswith('admin_rules:'))
async def admin_rules(callback:CallbackQuery):
    ch=db.get_challenge(int(callback.data.split(':')[1]))
    if not await can_manage_challenge(callback.from_user.id,ch):return await callback.answer('Недостаточно прав',show_alert=True)
    await callback.answer(); await callback.message.edit_text(rules_text(ch),reply_markup=admin_challenge_keyboard(ch.id))

@router.callback_query(F.data.startswith('admin_finish:'))
async def admin_finish_prompt(callback:CallbackQuery):
    ch=db.get_challenge(int(callback.data.split(':')[1]))
    if not await can_manage_challenge(callback.from_user.id,ch):return await callback.answer('Недостаточно прав',show_alert=True)
    await callback.answer(); await callback.message.edit_text(f'Завершить челлендж <b>«{escape(ch.title)}»</b>?\n\nПосле завершения новые результаты внести будет нельзя.',reply_markup=finish_confirm_keyboard(ch.id))

@router.callback_query(F.data.startswith('admin_finish_confirm:'))
async def admin_finish_confirm(callback:CallbackQuery,bot:Bot):
    ch=db.get_challenge(int(callback.data.split(':')[1]))
    if not await can_manage_challenge(callback.from_user.id,ch):return await callback.answer('Недостаточно прав',show_alert=True)
    if ch.status!='active':return await callback.answer('Челлендж уже завершён',show_alert=True)
    g=db.get_group(ch.group_id); final=ranking_text(ch); db.finish_challenge(ch.id); text=f'🏁 Челлендж «{escape(ch.title)}» завершён.\n\n{final}'
    await callback.answer('Челлендж завершён'); await callback.message.edit_text(text)
    if g:
        try: await bot.send_message(g.telegram_chat_id,text)
        except Exception: logging.exception('Не удалось отправить итог в группу %s',g.telegram_chat_id)


@router.message(F.text=='➕ Новый челлендж')
async def new_challenge(message:Message,state:FSMContext):
    g=db.get_selected_group(message.from_user.id)
    if not g or not (db.is_group_admin(g.id,message.from_user.id) or message.from_user.id in ADMIN_IDS):return
    if db.get_active_challenge(g.id):return await message.answer('Сначала завершите текущий активный челлендж.')
    await state.update_data(group_id=g.id,exercises=[]); await state.set_state(ChallengeForm.title); await message.answer('Название нового челленджа:',reply_markup=ReplyKeyboardRemove())

@router.message(ChallengeForm.title)
async def ch_title(message:Message,state:FSMContext):
    title=(message.text or '').strip()
    if not title:return await message.answer('Введите название.')
    await state.update_data(title=title); await state.set_state(ChallengeForm.start_date); await message.answer('Дата начала в формате ГГГГ-ММ-ДД:')

@router.message(ChallengeForm.start_date)
async def ch_start(message:Message,state:FSMContext):
    try:d=date.fromisoformat((message.text or '').strip())
    except ValueError:return await message.answer('Неверная дата.')
    await state.update_data(start_date=d.isoformat()); await state.set_state(ChallengeForm.duration); await message.answer('Продолжительность в днях, от 1 до 365:')

@router.message(ChallengeForm.duration)
async def ch_duration(message:Message,state:FSMContext):
    try:v=int((message.text or '').strip()); assert 1<=v<=365
    except Exception:return await message.answer('Введите число от 1 до 365.')
    await state.update_data(duration=v); await state.set_state(ChallengeForm.exercise_name); await message.answer('Название первого упражнения, например «Планка»:')

@router.message(ChallengeForm.exercise_name)
async def ex_name(message:Message,state:FSMContext):
    name=(message.text or '').strip()
    if not name:return await message.answer('Введите название упражнения.')
    await state.update_data(current_name=name); await state.set_state(ChallengeForm.exercise_unit); await message.answer('Единица измерения: раз, минут, км, шагов и т. п.:')

@router.message(ChallengeForm.exercise_unit)
async def ex_unit(message:Message,state:FSMContext):
    unit=(message.text or '').strip()
    if not unit:return await message.answer('Введите единицу измерения.')
    await state.update_data(current_unit=unit); await state.set_state(ChallengeForm.exercise_target); await message.answer('Дневная норма числом:')

@router.message(ChallengeForm.exercise_target)
async def ex_target(message:Message,state:FSMContext):
    try:v=float((message.text or '').replace(',','.')); assert v>0
    except Exception:return await message.answer('Введите положительное число.')
    await state.update_data(current_target=v); await state.set_state(ChallengeForm.exercise_points); await message.answer('Сколько максимум баллов даёт выполнение нормы? Например 1:')

@router.message(ChallengeForm.exercise_points)
async def ex_points(message:Message,state:FSMContext):
    try:p=float((message.text or '').replace(',','.')); assert p>0
    except Exception:return await message.answer('Введите положительное число.')
    d=await state.get_data(); exercises=d['exercises']+[{'name':d['current_name'],'unit':d['current_unit'],'target':d['current_target'],'points':p}]
    await state.update_data(exercises=exercises); await state.set_state(ChallengeForm.exercise_more)
    kb=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='➕ Добавить ещё'),KeyboardButton(text='✅ Создать челлендж')]],resize_keyboard=True)
    await message.answer(f'Добавлено: <b>{escape(d["current_name"])}</b>. Добавить ещё упражнение?',reply_markup=kb)

@router.message(ChallengeForm.exercise_more,F.text=='➕ Добавить ещё')
async def ex_more(message:Message,state:FSMContext): await state.set_state(ChallengeForm.exercise_name); await message.answer('Название следующего упражнения:',reply_markup=ReplyKeyboardRemove())

@router.message(ChallengeForm.exercise_more,F.text=='✅ Создать челлендж')
async def choose_rules(message:Message,state:FSMContext):
    await state.set_state(ChallengeForm.rule_mode)
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='⚖️ Классический',callback_data='rule_template:classic')],
        [InlineKeyboardButton(text='✅ Выполнил / не выполнил',callback_data='rule_template:binary')],
        [InlineKeyboardButton(text='📶 Ступенчатый',callback_data='rule_template:step')],
        [InlineKeyboardButton(text='⚙️ Настроить самостоятельно',callback_data='rule_template:custom')],
    ])
    await message.answer('Выберите правила начисления:',reply_markup=kb)

async def finalize_challenge(message:Message,state:FSMContext,rules:dict):
    d=await state.get_data(); start=date.fromisoformat(d['start_date']); end=start+timedelta(days=int(d['duration'])-1); g=db.get_group(d['group_id'])
    try: cid=db.create_challenge(g.id,d['title'],start.isoformat(),end.isoformat(),g.telegram_chat_id,d['exercises'],rules)
    except sqlite3.IntegrityError: return await message.answer('Активный челлендж уже существует.')
    await state.clear(); ch=db.get_challenge(cid); await message.answer('✅ Челлендж создан.\n\n'+rules_text(ch),reply_markup=main_keyboard())

@router.callback_query(ChallengeForm.rule_mode,F.data.startswith('rule_template:'))
async def rule_template(callback:CallbackQuery,state:FSMContext):
    template=callback.data.split(':')[1]; await callback.answer()
    presets={
      'classic':dict(scoring_mode='proportional',over_target_mode='stats_only',success_mode='all_targets',min_daily_points=0,edit_days=1,join_mode='open'),
      'binary':dict(scoring_mode='binary',over_target_mode='stats_only',success_mode='all_targets',min_daily_points=0,edit_days=1,join_mode='open'),
      'step':dict(scoring_mode='step',over_target_mode='stats_only',success_mode='min_points',min_daily_points=1,edit_days=1,join_mode='open'),
    }
    if template in presets: return await finalize_challenge(callback.message,state,presets[template])
    kb=InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text='Пропорционально',callback_data='custom_score:proportional')],
      [InlineKeyboardButton(text='Только полная норма',callback_data='custom_score:binary')],
      [InlineKeyboardButton(text='Ступенчато 50% / 100%',callback_data='custom_score:step')],
      [InlineKeyboardButton(text='За любой результат',callback_data='custom_score:fixed')]])
    await callback.message.answer('Как начислять баллы?',reply_markup=kb)

@router.callback_query(ChallengeForm.rule_mode,F.data.startswith('custom_score:'))
async def custom_score(callback:CallbackQuery,state:FSMContext):
    await state.update_data(scoring_mode=callback.data.split(':')[1]); await state.set_state(ChallengeForm.over_target); await callback.answer()
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Показывать в статистике, баллы ограничить',callback_data='over:stats_only')],[InlineKeyboardButton(text='Не учитывать сверх нормы',callback_data='over:cap')]])
    await callback.message.answer('Что делать с перевыполнением нормы?',reply_markup=kb)

@router.callback_query(ChallengeForm.over_target,F.data.startswith('over:'))
async def custom_over(callback:CallbackQuery,state:FSMContext):
    await state.update_data(over_target_mode=callback.data.split(':')[1]); await state.set_state(ChallengeForm.success_mode); await callback.answer()
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Все нормы выполнены',callback_data='success:all_targets')],[InlineKeyboardButton(text='Есть любой результат',callback_data='success:any')],[InlineKeyboardButton(text='Минимум баллов за день',callback_data='success:min_points')]])
    await callback.message.answer('Что считать успешным днём?',reply_markup=kb)

@router.callback_query(ChallengeForm.success_mode,F.data.startswith('success:'))
async def custom_success(callback:CallbackQuery,state:FSMContext):
    mode=callback.data.split(':')[1]; await state.update_data(success_mode=mode); await callback.answer()
    if mode=='min_points': await state.set_state(ChallengeForm.min_points); return await callback.message.answer('Введите минимальное количество баллов для успешного дня:')
    await state.update_data(min_daily_points=0); await state.set_state(ChallengeForm.edit_days); await callback.message.answer('Сколько прошлых дней можно исправлять? Введите 0–14:')

@router.message(ChallengeForm.min_points)
async def custom_min_points(message:Message,state:FSMContext):
    try:v=float((message.text or '').replace(',','.')); assert v>=0
    except Exception:return await message.answer('Введите число не меньше нуля.')
    await state.update_data(min_daily_points=v); await state.set_state(ChallengeForm.edit_days); await message.answer('Сколько прошлых дней можно исправлять? Введите 0–14:')

@router.message(ChallengeForm.edit_days)
async def custom_edit_days(message:Message,state:FSMContext):
    try:v=int((message.text or '').strip()); assert 0<=v<=14
    except Exception:return await message.answer('Введите целое число от 0 до 14.')
    await state.update_data(edit_days=v); await state.set_state(ChallengeForm.join_mode)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Можно вступать после старта',callback_data='join:open')],[InlineKeyboardButton(text='Только до старта',callback_data='join:before_start')],[InlineKeyboardButton(text='Только через администратора',callback_data='join:manual')]])
    await message.answer('Как участники вступают в челлендж?',reply_markup=kb)

@router.callback_query(ChallengeForm.join_mode,F.data.startswith('join:'))
async def custom_join(callback:CallbackQuery,state:FSMContext):
    await state.update_data(join_mode=callback.data.split(':')[1]); d=await state.get_data(); await callback.answer()
    rules={k:d[k] for k in ('scoring_mode','over_target_mode','success_mode','min_daily_points','edit_days','join_mode')}
    await finalize_challenge(callback.message,state,rules)


@router.message(F.text=='🗂 Архив челленджей')
async def archive(message:Message):
    if not private(message) or not message.from_user:return
    g=db.get_selected_group(message.from_user.id)
    if not g:return await message.answer('Сначала выберите группу.')
    if not (db.is_group_admin(g.id,message.from_user.id) or message.from_user.id in ADMIN_IDS):return await message.answer('Недостаточно прав.')
    rows=db.get_finished_challenges(g.id,20)
    if not rows:return await message.answer('Архив пока пуст.')
    lines=[f'<b>🗂 Архив группы «{escape(g.title)}»</b>','']
    for r in rows:
        lines.append(f'• <b>{escape(r["title"])}</b>\n  {date.fromisoformat(r["start_date"]):%d.%m.%Y} — {date.fromisoformat(r["end_date"]):%d.%m.%Y} · участников: {r["members"]}')
    await message.answer('\n'.join(lines))

@router.message(F.text=='📋 Копировать челлендж')
async def clone_start(message:Message,state:FSMContext):
    g=db.get_selected_group(message.from_user.id)
    if not g:return
    if db.get_active_challenge(g.id):return await message.answer('Сначала завершите текущий активный челлендж.')
    rows=db.get_challenges(g.id)
    if not rows:return await message.answer('Нечего копировать.')
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=r['title'],callback_data=f'clone:{r["id"]}')] for r in rows])
    await state.set_state(CloneForm.choose); await message.answer('Выберите челлендж-образец:',reply_markup=kb)

@router.callback_query(CloneForm.choose,F.data.startswith('clone:'))
async def clone_choose(callback:CallbackQuery,state:FSMContext):
    source=int(callback.data.split(':')[1]); await state.update_data(source_id=source); await state.set_state(CloneForm.title); await callback.answer(); await callback.message.answer('Название нового челленджа:')

@router.message(CloneForm.title)
async def clone_title(message:Message,state:FSMContext): await state.update_data(title=(message.text or '').strip()); await state.set_state(CloneForm.start_date); await message.answer('Дата начала ГГГГ-ММ-ДД:')

@router.message(CloneForm.start_date)
async def clone_date(message:Message,state:FSMContext):
    try:d=date.fromisoformat((message.text or '').strip())
    except ValueError:return await message.answer('Неверная дата.')
    await state.update_data(start_date=d.isoformat()); await state.set_state(CloneForm.duration); await message.answer('Продолжительность в днях:')

@router.message(CloneForm.duration)
async def clone_duration(message:Message,state:FSMContext):
    try:duration=int((message.text or '').strip()); assert 1<=duration<=365
    except Exception:return await message.answer('Введите число от 1 до 365.')
    d=await state.get_data(); g=db.get_selected_group(message.from_user.id); start=date.fromisoformat(d['start_date']); end=start+timedelta(days=duration-1)
    cid=db.clone_challenge(d['source_id'],g.id,d['title'],start.isoformat(),end.isoformat(),g.telegram_chat_id); await state.clear(); await message.answer('✅ Копия создана.\n\n'+rules_text(db.get_challenge(cid)),reply_markup=main_keyboard())

@router.message(F.text=='🏁 Завершить челлендж')
async def finish(message:Message):
    if not private(message) or not message.from_user:return
    g,ch=selected(message.from_user.id)
    if not g or not ch:return await message.answer('Нет активного челленджа.')
    if not (db.is_group_admin(g.id,message.from_user.id) or message.from_user.id in ADMIN_IDS):return await message.answer('Недостаточно прав.')
    await message.answer(f'Завершить челлендж <b>«{escape(ch.title)}»</b>?\n\nПосле завершения новые результаты внести будет нельзя.',reply_markup=finish_confirm_keyboard(ch.id))

@router.message(Command('id'))
async def ids(message:Message): await message.answer(f'ID чата: <code>{message.chat.id}</code>')

# В группах бот обрабатывает только явно зарегистрированные команды и кнопки.
# Обычные сообщения участников и неизвестные команды намеренно игнорируются,
# чтобы бот не вмешивался в общение и не засорял чат.
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def ignore_group_messages(message: Message):
    return

async def main():
    logging.basicConfig(level=logging.INFO); db.init(); bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML)); dp=Dispatcher(storage=MemoryStorage()); dp.include_router(router)
    await bot.set_my_commands([BotCommand(command='start',description='Открыть меню'),BotCommand(command='rating',description='Рейтинг группы'),BotCommand(command='stats',description='Статистика группы'),BotCommand(command='id',description='ID чата')])
    await dp.start_polling(bot)

if __name__=='__main__': asyncio.run(main())
