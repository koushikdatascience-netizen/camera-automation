from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class TrackIdentity:
    first_seen:datetime; last_seen:datetime; attempts:int=0; votes:dict=field(default_factory=dict); best_similarity:float=0.0; state:str='UNRESOLVED'; person_id:str|None=None; confidence:float=0.0

class IdentityResolutionEngine:
    def __init__(self,cfg): self.cfg=cfg; self.tracks={}
    def observe(self,camera_id,track_id,ts,match_person_id,score,valid_face=True):
        key=(camera_id,str(track_id)); st=self.tracks.get(key) or TrackIdentity(ts,ts); self.tracks[key]=st; st.last_seen=ts
        if not valid_face: return st
        st.attempts+=1; st.best_similarity=max(st.best_similarity,score)
        if match_person_id:
            st.votes[match_person_id]=st.votes.get(match_person_id,0)+1
            if st.votes[match_person_id]>=self.cfg.required_known_observations and score>=self.cfg.known_threshold:
                st.state='KNOWN'; st.person_id=match_person_id; st.confidence=score
        else:
            elapsed=(ts-st.first_seen).total_seconds()
            if st.attempts>=self.cfg.max_recognition_attempts and elapsed>=self.cfg.unknown_confirmation_seconds: st.state='UNKNOWN'
        return st
    def forget(self,camera_id,track_id): self.tracks.pop((camera_id,str(track_id)),None)
